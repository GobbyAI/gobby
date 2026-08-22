"""Tests for GitHubSyncService class.

Tests verify the sync service correctly orchestrates between gobby tasks
and GitHub via the official GitHub MCP server.

TDD Red Phase: Tests should fail initially since GitHubSyncService class does not exist.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.sync.github import GitHubSyncService
from gobby.sync.tasks import _github_issue_uuid_seed

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("repo", "expected_repo"),
    [("audit", "audit"), ("gobby.git", "gobby")],
)
def test_github_issue_seed_removes_only_exact_git_suffix(repo: str, expected_repo: str) -> None:
    assert _github_issue_uuid_seed("project-id", "Owner", repo, 7) == (
        f"project-id/github/owner/{expected_repo}/issues/7"
    )


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCPClientManager."""
    manager = MagicMock()
    manager.has_server = MagicMock(return_value=True)
    manager.health = {"github": MagicMock(state="connected")}
    manager.call_tool = AsyncMock()
    return manager


@pytest.fixture
def mock_task_manager():
    """Create a mock LocalTaskManager."""
    manager = MagicMock()
    manager.create_task = MagicMock()
    manager.update_task = MagicMock()
    manager.reconcile_task_state = MagicMock()
    manager.get_task = MagicMock()
    manager.list_tasks = MagicMock(return_value=[])
    # Mock db.execute() for dedup queries — return no existing tasks by default
    manager.db.execute.return_value.fetchone.return_value = None
    return manager


@pytest.fixture
def sync_service(mock_mcp_manager, mock_task_manager):
    """Create a GitHubSyncService with mock dependencies."""
    return GitHubSyncService(
        mcp_manager=mock_mcp_manager,
        task_manager=mock_task_manager,
        project_id="test-project-id",
    )


class TestGitHubSyncServiceInit:
    """Test GitHubSyncService initialization."""

    def test_init_with_dependencies(self, mock_mcp_manager, mock_task_manager) -> None:
        """GitHubSyncService initializes with mcp_manager and task_manager."""
        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.mcp_manager is mock_mcp_manager
        assert service.task_manager is mock_task_manager
        assert service.project_id == "test-project"

    def test_init_creates_github_integration(self, mock_mcp_manager, mock_task_manager) -> None:
        """GitHubSyncService creates GitHubIntegration for availability checks."""
        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert hasattr(service, "github")
        # Should be a GitHubIntegration instance
        from gobby.integrations.github import GitHubIntegration

        assert isinstance(service.github, GitHubIntegration)

    def test_init_default_repo_is_none(self, mock_mcp_manager, mock_task_manager) -> None:
        """Default github_repo is None if not specified."""
        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.github_repo is None

    def test_init_with_github_repo(self, mock_mcp_manager, mock_task_manager) -> None:
        """GitHubSyncService accepts github_repo parameter."""
        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            github_repo="owner/repo",
        )
        assert service.github_repo == "owner/repo"


class TestGitHubSyncServiceAvailability:
    """Test availability checking."""

    def test_requires_github_available(self, mock_mcp_manager, mock_task_manager) -> None:
        """Operations should check GitHub availability first."""
        mock_mcp_manager.has_server.return_value = False

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        assert service.github.is_available() is False

    def test_is_available_proxies_to_integration(self, mock_mcp_manager, mock_task_manager) -> None:
        """is_available() delegates to GitHubIntegration."""
        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.is_available() == service.github.is_available()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_import_uses_lazy_real_manager_with_empty_health(self, mock_task_manager) -> None:
        manager = MCPClientManager(
            server_configs=[
                MCPServerConfig(
                    name="github",
                    project_id="test-project",
                    transport="stdio",
                    command="unused",
                )
            ]
        )
        session = MagicMock()
        session.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text="[]")],
                is_error=False,
            )
        )
        manager._connections["github"] = SimpleNamespace(is_connected=True, session=session)

        service = GitHubSyncService(manager, mock_task_manager, "test-project")

        assert manager.health == {}
        assert await service.import_github_issues("owner/repo") == []
        session.call_tool.assert_awaited_once_with(
            "list_issues",
            {
                "owner": "owner",
                "repo": "repo",
                "state": "open",
                "page": 1,
                "per_page": 100,
            },
        )


class TestGitHubSyncServiceImport:
    """Test import_github_issues method."""

    @pytest.mark.asyncio
    async def test_import_issues_calls_github_mcp(self, sync_service, mock_mcp_manager):
        """import_github_issues calls GitHub MCP list_issues tool."""
        mock_mcp_manager.call_tool.return_value = {"issues": []}

        await sync_service.import_github_issues(repo="owner/repo")

        mock_mcp_manager.call_tool.assert_called()
        # Verify GitHub MCP was called
        calls = mock_mcp_manager.call_tool.call_args_list
        assert any("github" in str(call) for call in calls)

    async def test_import_issues_paginates_until_short_page(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """import_github_issues imports every page of GitHub issues."""
        first_page = [{"number": number, "title": f"Issue {number}"} for number in range(1, 101)]
        second_page = [{"number": 101, "title": "Issue 101"}]
        mock_mcp_manager.call_tool.side_effect = [
            {"issues": first_page},
            {"issues": second_page},
        ]

        imported = await sync_service.import_github_issues(repo="owner/repo")

        assert len(imported) == 101
        assert mock_task_manager.create_task.call_count == 101
        page_args = [call.kwargs["arguments"] for call in mock_mcp_manager.call_tool.call_args_list]
        assert [(args["page"], args["per_page"]) for args in page_args] == [
            (1, 100),
            (2, 100),
        ]

    @pytest.mark.asyncio
    async def test_import_issues_creates_tasks(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """import_github_issues creates gobby tasks from GitHub issues."""
        mock_mcp_manager.call_tool.return_value = CallToolResult(
            content=[],
            structured_content={
                "issues": [
                    {"number": 1, "title": "Issue 1", "body": "Description 1"},
                    {"number": 2, "title": "Issue 2", "body": "Description 2"},
                ]
            },
            is_error=False,
        )

        await sync_service.import_github_issues(repo="owner/repo")

        # Should create tasks for each issue
        assert mock_task_manager.create_task.call_count >= 2

    @pytest.mark.asyncio
    async def test_import_issues_links_github_fields(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """import_github_issues sets github_issue_number and github_repo on tasks."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [{"number": 42, "title": "Test Issue", "body": "Test body"}]
        }

        await sync_service.import_github_issues(repo="owner/repo")

        # Verify task created with GitHub fields
        create_call = mock_task_manager.create_task.call_args
        assert create_call is not None
        kwargs = create_call.kwargs if create_call.kwargs else {}
        args_dict = (
            dict(zip(["project_id", "title"], create_call.args, strict=False))
            if create_call.args
            else {}
        )
        all_args = {**args_dict, **kwargs}

        assert all_args.get("github_issue_number") == 42 or "github_issue_number" in str(
            create_call
        )

    @pytest.mark.asyncio
    async def test_import_closed_issue_maps_labels_and_closes_task(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """Closed GitHub issues create closed tasks with mapped labels."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "number": 123,
                    "title": "Closed issue",
                    "state": "closed",
                    "closed_at": "2026-07-14T12:00:00Z",
                    "labels": [{"name": "gobby:bug"}],
                }
            ]
        }
        created = MagicMock(id="new-task-id")
        reconciled = MagicMock()
        reconciled.to_dict.return_value = {"id": "new-task-id"}
        mock_task_manager.create_task.return_value = created
        mock_task_manager.reconcile_task_state.return_value = reconciled

        await sync_service.import_github_issues(repo="owner/repo")

        assert mock_task_manager.create_task.call_args.kwargs["labels"] == ["gobby:bug"]
        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "new-task-id",
            closed_at="2026-07-14T12:00:00Z",
            closed_reason="github_sync",
            closed_in_session_id=None,
            closed_commit_sha=None,
        )

    @pytest.mark.asyncio
    async def test_import_issues_raises_when_unavailable(self, mock_mcp_manager, mock_task_manager):
        """import_github_issues raises RuntimeError when GitHub unavailable."""
        mock_mcp_manager.has_server.return_value = False

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        with pytest.raises(RuntimeError, match="GitHub"):
            await service.import_github_issues(repo="owner/repo")


class TestGitHubSyncServiceDedup:
    """Test import deduplication behavior."""

    @pytest.mark.asyncio
    async def test_import_updates_existing_task_instead_of_creating(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """Re-importing an issue updates the existing task instead of creating a duplicate."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [{"number": 1, "title": "Updated Title", "body": "Updated body"}]
        }

        # Simulate existing task found by dedup query
        mock_task_manager.db.execute.return_value.fetchone.return_value = {"id": "existing-task-id"}

        existing_task = MagicMock()
        existing_task.id = "existing-task-id"
        existing_task.title = "Original Title"
        existing_task.description = "Original body"
        existing_task.labels = []
        existing_task.updated_at = None
        existing_task.to_dict.return_value = {"id": "existing-task-id", "title": "Updated Title"}
        mock_task_manager.get_task.return_value = existing_task

        result = await sync_service.import_github_issues(repo="owner/repo")

        # Should update, not create
        mock_task_manager.update_task.assert_called_once()
        mock_task_manager.create_task.assert_not_called()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_import_preserves_newer_local_fields_and_labels(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "number": 1,
                    "title": "Stale remote title",
                    "body": "Stale remote body",
                    "labels": [],
                    "state": "closed",
                    "updated_at": "2026-02-11T12:00:00Z",
                }
            ]
        }
        mock_task_manager.db.execute.return_value.fetchone.return_value = {"id": "existing-task-id"}
        existing_task = MagicMock(
            id="existing-task-id",
            title="Newer local title",
            description="Newer local body",
            labels=["local-only"],
            updated_at="2026-02-12T12:00:00Z",
        )
        existing_task.to_dict.return_value = {
            "id": "existing-task-id",
            "title": "Newer local title",
            "labels": ["local-only"],
        }
        mock_task_manager.get_task.return_value = existing_task

        result = await sync_service.import_github_issues(repo="owner/repo")

        mock_task_manager.update_task.assert_not_called()
        mock_task_manager.reconcile_task_state.assert_not_called()
        assert result == [existing_task.to_dict.return_value]

    @pytest.mark.asyncio
    async def test_import_applies_newer_remote_fields_and_merges_labels(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "number": 1,
                    "title": "Newer remote title",
                    "body": "Newer remote body",
                    "labels": [{"name": "remote"}],
                    "updated_at": "2026-02-12T12:00:00Z",
                }
            ]
        }
        mock_task_manager.db.execute.return_value.fetchone.return_value = {"id": "existing-task-id"}
        existing_task = MagicMock(
            id="existing-task-id",
            title="Old local title",
            description="Old local body",
            labels=["local-only"],
            updated_at="2026-02-11T12:00:00Z",
        )
        existing_task.to_dict.return_value = {"id": "existing-task-id"}
        mock_task_manager.get_task.return_value = existing_task

        result = await sync_service.import_github_issues(repo="owner/repo")

        mock_task_manager.update_task.assert_called_once_with(
            "existing-task-id",
            title="Newer remote title",
            description="Newer remote body",
            validation_criteria=(
                "The acceptance conditions recorded in GitHub issue owner/repo#1 are "
                "implemented, and the resulting behavior is verified by authoritative "
                "current-state evidence."
            ),
            labels=["local-only", "remote"],
        )
        mock_task_manager.create_task.assert_not_called()
        assert result == [{"id": "existing-task-id"}]

    @pytest.mark.asyncio
    async def test_import_open_issue_reopens_existing_task(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """Open GitHub issues clear close metadata on existing tasks."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [{"number": 1, "title": "Reopened", "state": "open"}]
        }
        mock_task_manager.db.execute.return_value.fetchone.return_value = {"id": "existing-task-id"}
        existing_task = MagicMock(id="existing-task-id")
        existing_task.to_dict.return_value = {"id": "existing-task-id"}
        mock_task_manager.get_task.return_value = existing_task

        result = await sync_service.import_github_issues(repo="owner/repo")

        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "existing-task-id",
            closed_at=None,
            closed_reason=None,
            closed_in_session_id=None,
            closed_commit_sha=None,
        )
        assert result == [{"id": "existing-task-id"}]

    @pytest.mark.asyncio
    async def test_import_creates_new_task_when_no_existing(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """Importing a new issue creates a task when no existing task matches."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [{"number": 99, "title": "New Issue", "body": "New body"}]
        }

        result = await sync_service.import_github_issues(repo="owner/repo")

        mock_task_manager.create_task.assert_called_once()
        assert len(result) == 1


class TestGitHubSyncServiceSync:
    """Test sync_task_to_github method."""

    @pytest.mark.asyncio
    async def test_sync_task_calls_github_mcp(self, sync_service, mock_mcp_manager):
        """sync_task_to_github calls GitHub MCP to update issue."""
        mock_task = MagicMock()
        mock_task.github_issue_number = 42
        mock_task.github_repo = "owner/repo"
        mock_task.title = "Updated Title"
        mock_task.description = "Updated description"
        mock_task.closed_at = "2026-07-14T12:00:00Z"
        mock_task.labels = ["bug", "priority:high"]

        sync_service.task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text='{"success":true}')],
            is_error=False,
        )

        await sync_service.sync_task_to_github(task_id="test-task-id")

        mock_mcp_manager.call_tool.assert_called()
        assert mock_mcp_manager.call_tool.call_count >= 1
        assert mock_mcp_manager.call_tool.call_args is not None
        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["state"] == "closed"
        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["labels"] == [
            "bug",
            "priority:high",
        ]

    @pytest.mark.asyncio
    async def test_sync_open_task_reopens_github_issue(self, sync_service, mock_mcp_manager):
        """An open Gobby task pushes GitHub issue state as open."""
        task = SimpleNamespace(
            github_issue_number=42,
            github_repo="owner/repo",
            title="Reopened task",
            description=None,
            closed_at=None,
            labels=[],
        )
        sync_service.task_manager.get_task.return_value = task
        mock_mcp_manager.call_tool.return_value = {"success": True}

        await sync_service.sync_task_to_github(task_id="test-task-id")

        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["state"] == "open"

    @pytest.mark.asyncio
    async def test_sync_task_raises_when_no_issue_number(self, sync_service, mock_task_manager):
        """sync_task_to_github raises ValueError when task has no github_issue_number."""
        mock_task = MagicMock()
        mock_task.github_issue_number = None

        mock_task_manager.get_task.return_value = mock_task

        with pytest.raises(ValueError, match="issue"):
            await sync_service.sync_task_to_github(task_id="test-task-id")


class TestGitHubSyncServicePR:
    """Test create_pr_for_task method."""

    @pytest.mark.asyncio
    async def test_create_pr_calls_github_mcp(self, sync_service, mock_mcp_manager):
        """create_pr_for_task calls GitHub MCP create_pull_request."""
        mock_task = MagicMock()
        mock_task.title = "Feature: Add new thing"
        mock_task.description = "Adds a cool feature"
        mock_task.github_repo = "owner/repo"
        mock_task.id = "test-task-id"

        sync_service.task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {
            "number": 123,
            "url": "https://github.com/owner/repo/pull/123",
        }

        result = await sync_service.create_pr_for_task(
            task_id="test-task-id",
            head_branch="feature/new-thing",
            base_branch="main",
        )

        mock_mcp_manager.call_tool.assert_called()
        assert result is not None

    @pytest.mark.asyncio
    async def test_create_pr_updates_task_pr_number(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """create_pr_for_task updates task with github_pr_number."""
        mock_task = MagicMock()
        mock_task.title = "Feature"
        mock_task.description = "Description"
        mock_task.github_repo = "owner/repo"
        mock_task.id = "test-task-id"

        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = CallToolResult(
            content=[],
            structured_content={"number": 456},
            is_error=False,
        )

        await sync_service.create_pr_for_task(
            task_id="test-task-id",
            head_branch="feature/thing",
            base_branch="main",
        )

        # Should update task with PR number
        mock_task_manager.update_task.assert_called()
        update_call = mock_task_manager.update_task.call_args
        assert update_call is not None


class TestLabelMapping:
    """Test label mapping functions."""

    def test_map_gobby_labels_to_github_basic(self, sync_service) -> None:
        """map_gobby_labels_to_github converts internal labels to GitHub format."""
        gobby_labels = ["bug", "high-priority", "backend"]
        github_labels = sync_service.map_gobby_labels_to_github(gobby_labels)

        assert isinstance(github_labels, list)
        assert len(github_labels) == 3

    def test_map_gobby_labels_to_github_empty(self, sync_service) -> None:
        """map_gobby_labels_to_github handles empty labels."""
        github_labels = sync_service.map_gobby_labels_to_github([])
        assert github_labels == []

    def test_map_gobby_labels_to_github_with_prefix(self, sync_service) -> None:
        """map_gobby_labels_to_github can add prefix to labels."""
        gobby_labels = ["bug"]
        github_labels = sync_service.map_gobby_labels_to_github(gobby_labels, prefix="gobby:")
        assert "gobby:bug" in github_labels

    def test_map_github_labels_to_gobby_basic(self, sync_service) -> None:
        """map_github_labels_to_gobby parses GitHub labels to internal format."""
        github_labels = ["bug", "enhancement", "documentation"]
        gobby_labels = sync_service.map_github_labels_to_gobby(github_labels)

        assert isinstance(gobby_labels, list)
        assert len(gobby_labels) == 3

    def test_map_github_labels_to_gobby_empty(self, sync_service) -> None:
        """map_github_labels_to_gobby handles empty labels."""
        gobby_labels = sync_service.map_github_labels_to_gobby([])
        assert gobby_labels == []

    def test_map_github_labels_to_gobby_strips_prefix(self, sync_service) -> None:
        """map_github_labels_to_gobby strips gobby: prefix."""
        github_labels = ["gobby:bug", "gobby:feature"]
        gobby_labels = sync_service.map_github_labels_to_gobby(github_labels, strip_prefix="gobby:")
        assert "bug" in gobby_labels
        assert "feature" in gobby_labels

    def test_map_labels_special_characters(self, sync_service) -> None:
        """Label mapping handles special characters in label names."""
        gobby_labels = ["feature/new-ui", "p0:critical"]
        github_labels = sync_service.map_gobby_labels_to_github(gobby_labels)

        # Should preserve special characters
        assert len(github_labels) == 2


class TestGitHubSyncIntegration:
    """Integration tests for full GitHubSyncService workflows."""

    @pytest.mark.asyncio
    async def test_import_and_sync_workflow(self, mock_mcp_manager, mock_task_manager):
        """Test full workflow: import issues, then sync back."""
        # Setup: GitHub MCP returns realistic issue data
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        # First call returns issues, second call updates issue
        mock_mcp_manager.call_tool.side_effect = [
            # list_issues response
            {
                "issues": [
                    {
                        "number": 42,
                        "title": "Original Title",
                        "body": "Original description",
                        "labels": [{"name": "bug"}],
                        "state": "open",
                    }
                ]
            },
            # update_issue response
            {"number": 42, "title": "Updated Title", "state": "open"},
        ]

        # Create mock task with github fields
        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.github_issue_number = 42
        mock_task.github_repo = "owner/repo"
        mock_task.title = "Updated Title"
        mock_task.description = "Updated description"
        mock_task_manager.create_task.return_value = mock_task
        mock_task_manager.get_task.return_value = mock_task

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        # Import issues
        imported = await service.import_github_issues(repo="owner/repo")
        assert len(imported) == 1

        # Sync back to GitHub
        result = await service.sync_task_to_github(task_id="gt-test123")
        assert result is not None

    @pytest.mark.asyncio
    async def test_create_pr_after_task_completion(self, mock_mcp_manager, mock_task_manager):
        """Test workflow: complete task and create PR."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.return_value = {
            "number": 123,
            "url": "https://github.com/owner/repo/pull/123",
            "state": "open",
        }

        mock_task = MagicMock()
        mock_task.id = "gt-completed"
        mock_task.title = "Implement feature X"
        mock_task.description = "Added feature X as requested"
        mock_task.github_repo = "owner/repo"
        mock_task.github_issue_number = 42
        mock_task_manager.get_task.return_value = mock_task

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        result = await service.create_pr_for_task(
            task_id="gt-completed",
            head_branch="feature/x",
            base_branch="main",
        )

        assert result["number"] == 123
        # Verify task was updated with PR number
        mock_task_manager.update_task.assert_called()

    @pytest.mark.asyncio
    async def test_error_recovery_network_failure(self, mock_mcp_manager, mock_task_manager):
        """Test error handling when network fails."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.side_effect = Exception("Network error")

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        # Should raise the network error
        with pytest.raises(Exception, match="Network error"):
            await service.import_github_issues(repo="owner/repo")

    @pytest.mark.asyncio
    async def test_handles_empty_issue_list(self, mock_mcp_manager, mock_task_manager):
        """Test handling of repo with no issues."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.return_value = {"issues": []}

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        result = await service.import_github_issues(repo="owner/repo")
        assert result == []
        mock_task_manager.create_task.assert_not_called()


class TestGitHubSyncExceptions:
    """Test custom exceptions and error handling."""

    def test_github_sync_error_base_exception(self) -> None:
        """GitHubSyncError is a base exception for sync errors."""
        from gobby.sync.github import GitHubSyncError

        error = GitHubSyncError("Something went wrong")
        assert str(error) == "Something went wrong"
        assert isinstance(error, Exception)


class TestGitHubSyncErrorHandling:
    """Test error handling in sync operations."""

    @pytest.mark.asyncio
    async def test_sync_validates_response_structure(self, mock_mcp_manager, mock_task_manager):
        """sync_task_to_github validates response before processing."""
        from gobby.sync.github import GitHubSyncError

        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}
        # Invalid response structure
        mock_mcp_manager.call_tool.return_value = None

        mock_task = MagicMock()
        mock_task.github_issue_number = 42
        mock_task.github_repo = "owner/repo"
        mock_task.title = "Test"
        mock_task.description = "Test desc"
        mock_task_manager.get_task.return_value = mock_task

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        # Should handle gracefully or raise appropriate error
        with pytest.raises((GitHubSyncError, TypeError, AttributeError)):
            await service.sync_task_to_github(task_id="test-task")

    @pytest.mark.asyncio
    async def test_error_includes_context(self, mock_mcp_manager, mock_task_manager):
        """Errors include context about the operation."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.side_effect = Exception("API error")

        service = GitHubSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        try:
            await service.import_github_issues(repo="owner/repo")
        except Exception as e:
            # Error should contain useful context
            assert "error" in str(e).lower() or "API" in str(e)

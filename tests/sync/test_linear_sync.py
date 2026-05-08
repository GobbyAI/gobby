"""Tests for LinearSyncService class.

Tests verify the sync service correctly orchestrates between gobby tasks
and Linear via the official Linear MCP server.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.sync.linear import LinearSyncService

pytestmark = pytest.mark.unit


def _set_task_state(task: MagicMock, state: str) -> None:
    task.closed_at = None
    task.escalated_at = None
    task.is_escalated = False
    task.current_stage = {"state": state}


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCPClientManager."""
    manager = MagicMock()
    manager.has_server = MagicMock(return_value=True)
    manager.health = {"linear": MagicMock(state="connected")}
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
    # Default: no existing tasks (dedup check returns None)
    manager.db.fetchone.return_value = None
    manager.db.fetchall.return_value = []
    return manager


@pytest.fixture
def sync_service(mock_mcp_manager, mock_task_manager):
    """Create a LinearSyncService with mock dependencies."""
    project = MagicMock()
    project.linear_project_id = "lin-proj"
    project.name = "gobby"
    project.repo_path = None
    project_manager = MagicMock()
    project_manager.get.return_value = project
    return LinearSyncService(
        mcp_manager=mock_mcp_manager,
        task_manager=mock_task_manager,
        project_id="test-project-id",
        linear_team_id="team-123",
        project_manager=project_manager,
    )


class TestLinearSyncServiceInit:
    """Test LinearSyncService initialization."""

    def test_init_with_dependencies(self, mock_mcp_manager, mock_task_manager) -> None:
        """LinearSyncService initializes with mcp_manager and task_manager."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.mcp_manager is mock_mcp_manager
        assert service.task_manager is mock_task_manager
        assert service.project_id == "test-project"

    def test_init_creates_linear_integration(self, mock_mcp_manager, mock_task_manager) -> None:
        """LinearSyncService creates LinearIntegration for availability checks."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert hasattr(service, "linear")
        from gobby.integrations.linear import LinearIntegration

        assert isinstance(service.linear, LinearIntegration)

    def test_init_default_team_id_is_none(self, mock_mcp_manager, mock_task_manager) -> None:
        """Default linear_team_id is None if not specified."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.linear_team_id is None

    def test_init_with_team_id(self, mock_mcp_manager, mock_task_manager) -> None:
        """LinearSyncService accepts linear_team_id parameter."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-abc",
        )
        assert service.linear_team_id == "team-abc"

    def test_init_with_project_id(self, mock_mcp_manager, mock_task_manager) -> None:
        """LinearSyncService accepts linear_project_id parameter."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_project_id="lin-proj",
        )
        assert service.linear_project_id == "lin-proj"


class TestLinearSyncServiceAvailability:
    """Test availability checking."""

    def test_requires_linear_available(self, mock_mcp_manager, mock_task_manager) -> None:
        """Operations should check Linear availability first."""
        mock_mcp_manager.has_server.return_value = False

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        assert service.linear.is_available() is False

    def test_is_available_proxies_to_integration(self, mock_mcp_manager, mock_task_manager) -> None:
        """is_available() delegates to LinearIntegration."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.is_available() == service.linear.is_available()


class TestLinearSyncServiceImport:
    """Test import_linear_issues method."""

    @pytest.mark.asyncio
    async def test_import_issues_calls_linear_mcp(self, sync_service, mock_mcp_manager):
        """import_linear_issues calls Linear MCP list_issues tool."""
        mock_mcp_manager.call_tool.return_value = {"issues": []}

        await sync_service.import_linear_issues(team_id="team-123")

        mock_mcp_manager.call_tool.assert_called()
        calls = mock_mcp_manager.call_tool.call_args_list
        assert any("linear" in str(call) for call in calls)

    @pytest.mark.asyncio
    async def test_import_issues_creates_tasks(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """import_linear_issues creates gobby tasks from Linear issues."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {"id": "issue-1", "title": "Issue 1", "description": "Description 1"},
                {"id": "issue-2", "title": "Issue 2", "description": "Description 2"},
            ]
        }

        await sync_service.import_linear_issues()

        assert mock_task_manager.create_task.call_count >= 2

    @pytest.mark.asyncio
    async def test_import_issues_links_linear_fields(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """import_linear_issues sets linear_issue_id and linear_team_id on tasks."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [{"id": "lin-42", "title": "Test Issue", "description": "Test body"}]
        }

        await sync_service.import_linear_issues()

        create_call = mock_task_manager.create_task.call_args
        assert create_call is not None
        kwargs = create_call.kwargs if create_call.kwargs else {}
        args_dict = (
            dict(zip(["project_id", "title"], create_call.args, strict=False))
            if create_call.args
            else {}
        )
        all_args = {**args_dict, **kwargs}

        assert all_args.get("linear_issue_id") == "lin-42" or "linear_issue_id" in str(create_call)

    @pytest.mark.asyncio
    async def test_import_links_existing_task_by_gobby_ref_title(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """import_linear_issues links #ref Linear titles to existing Gobby tasks."""
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "lin-42",
                    "title": "#42: Existing Feature",
                    "description": "Test body",
                }
            ]
        }
        mock_task = MagicMock()
        mock_task.to_dict.return_value = {"id": "task-42", "title": "Existing Feature"}
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.db.fetchone.side_effect = [None, {"id": "task-42"}]

        await sync_service.import_linear_issues()

        mock_task_manager.update_task.assert_called_with(
            "task-42",
            linear_issue_id="lin-42",
            linear_team_id="team-123",
        )
        assert mock_task_manager.update_task.call_count >= 1
        assert mock_task_manager.update_task.call_args is not None
        mock_task_manager.reconcile_task_state.assert_called_with(
            "task-42",
            title="Existing Feature",
            description="Test body",
            priority=2,
        )
        assert mock_task_manager.reconcile_task_state.call_count >= 1
        assert mock_task_manager.reconcile_task_state.call_args is not None
        mock_task_manager.create_task.assert_not_called()
        assert mock_task_manager.create_task.call_count == 0
        assert not mock_task_manager.create_task.called

    @pytest.mark.asyncio
    async def test_import_issues_raises_when_unavailable(self, mock_mcp_manager, mock_task_manager):
        """import_linear_issues raises RuntimeError when Linear unavailable."""
        mock_mcp_manager.has_server.return_value = False

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
        )

        with pytest.raises(RuntimeError, match="Linear"):
            await service.import_linear_issues()

    @pytest.mark.asyncio
    async def test_import_issues_raises_when_no_team_id(self, mock_mcp_manager, mock_task_manager):
        """import_linear_issues raises ValueError when no team_id provided."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            # No linear_team_id
        )

        with pytest.raises(ValueError, match="team_id"):
            await service.import_linear_issues()

    @pytest.mark.asyncio
    async def test_import_issues_scopes_to_linear_project(
        self, mock_mcp_manager, mock_task_manager
    ):
        """import_linear_issues passes projectId when project binding exists."""
        mock_mcp_manager.call_tool.return_value = {"issues": []}
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
            linear_project_id="lin-proj",
        )

        await service.import_linear_issues()

        call = mock_mcp_manager.call_tool.call_args
        assert call.kwargs["arguments"]["teamId"] == "team-123"
        assert call.kwargs["arguments"]["projectId"] == "lin-proj"


class TestLinearSyncServiceSync:
    """Test sync_task_to_linear method."""

    @pytest.mark.asyncio
    async def test_sync_task_calls_linear_mcp(self, sync_service, mock_mcp_manager):
        """sync_task_to_linear calls Linear MCP to update issue."""
        mock_task = MagicMock()
        mock_task.linear_issue_id = "lin-42"
        mock_task.linear_team_id = "team-123"
        mock_task.title = "Updated Title"
        mock_task.description = "Updated description"
        _set_task_state(mock_task, "in_progress")
        mock_task.priority = 2

        sync_service.task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {"success": True}

        await sync_service.sync_task_to_linear(task_id="test-task-id")

        mock_mcp_manager.call_tool.assert_called()
        assert mock_mcp_manager.call_tool.call_count >= 1
        assert mock_mcp_manager.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_sync_task_graphql_resolves_linear_state_id(self, sync_service, mock_mcp_manager):
        """GraphQL issue updates send Linear's workflow state id."""
        mock_task = MagicMock()
        mock_task.id = "test-task-id"
        mock_task.seq_num = 42
        mock_task.linear_issue_id = "lin-42"
        mock_task.linear_team_id = "team-123"
        mock_task.title = "Updated Title"
        mock_task.description = "Updated description"
        _set_task_state(mock_task, "in_progress")
        mock_task.priority = 2

        client = MagicMock()
        client.list_team_states = AsyncMock(
            return_value=[
                {"id": "state-todo", "name": "Todo"},
                {"id": "state-progress", "name": "In Progress"},
            ]
        )
        client.update_issue = AsyncMock(return_value={"id": "lin-42", "title": "#42: Updated"})
        sync_service._get_graphql_client = MagicMock(return_value=client)
        sync_service.task_manager.get_task.return_value = mock_task

        await sync_service.sync_task_to_linear(task_id="test-task-id")

        client.list_team_states.assert_awaited_once_with("team-123")
        assert client.list_team_states.await_count == 1
        assert client.list_team_states.await_args is not None
        client.update_issue.assert_awaited_once_with(
            issue_id="lin-42",
            title="#42: Updated Title",
            description="Updated description",
            priority=2,
            state_id="state-progress",
        )
        assert client.update_issue.await_count == 1
        assert client.update_issue.await_args is not None
        mock_mcp_manager.call_tool.assert_not_called()
        assert mock_mcp_manager.call_tool.call_count == 0
        assert not mock_mcp_manager.call_tool.called

    @pytest.mark.asyncio
    async def test_sync_task_raises_when_no_issue_id(self, sync_service, mock_task_manager):
        """sync_task_to_linear raises ValueError when task has no linear_issue_id."""
        mock_task = MagicMock()
        mock_task.linear_issue_id = None

        mock_task_manager.get_task.return_value = mock_task

        with pytest.raises(ValueError, match="issue"):
            await sync_service.sync_task_to_linear(task_id="test-task-id")

    @pytest.mark.asyncio
    async def test_pull_updates_scopes_to_linear_project(self, mock_mcp_manager, mock_task_manager):
        """pull_linear_updates passes projectId when project binding exists."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": "task-1", "linear_issue_id": "issue-1"}
        ]
        mock_mcp_manager.call_tool.return_value = {"issues": []}
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
            linear_project_id="lin-proj",
        )

        await service.pull_linear_updates()

        call = mock_mcp_manager.call_tool.call_args
        assert call.kwargs["arguments"]["teamId"] == "team-123"
        assert call.kwargs["arguments"]["projectId"] == "lin-proj"

    @pytest.mark.asyncio
    async def test_sync_all_does_not_update_cursor_when_pull_has_errors(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all preserves the cursor when Linear pull fails."""
        sync_service.pull_linear_updates = AsyncMock(
            return_value={"updated": 0, "skipped": 0, "errors": 2}
        )
        sync_service.push_dirty_tasks = AsyncMock(
            return_value={"pushed": 0, "skipped": 0, "errors": 0}
        )
        sync_service._get_project_synced_at = MagicMock(return_value="old-cursor")
        sync_service._update_synced_at = MagicMock()

        result = await sync_service.sync_all(team_id="team-123")

        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        sync_service._update_synced_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_does_not_update_cursor_when_push_has_errors(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all preserves the cursor when Linear push fails."""
        sync_service.pull_linear_updates = AsyncMock(
            return_value={"updated": 1, "skipped": 0, "errors": 0}
        )
        sync_service.push_dirty_tasks = AsyncMock(
            return_value={"pushed": 0, "skipped": 0, "errors": 1}
        )
        sync_service._get_project_synced_at = MagicMock(return_value="old-cursor")
        sync_service._update_synced_at = MagicMock()

        result = await sync_service.sync_all(team_id="team-123")

        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        sync_service._update_synced_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_updates_cursor_when_pull_and_push_succeed(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all advances the cursor after an error-free bidirectional sync."""
        sync_service.pull_linear_updates = AsyncMock(
            return_value={"updated": 0, "skipped": 1, "errors": 0}
        )
        sync_service.push_dirty_tasks = AsyncMock(
            return_value={"pushed": 1, "skipped": 0, "errors": 0}
        )
        sync_service._update_synced_at = MagicMock()

        result = await sync_service.sync_all(team_id="team-123")

        assert result["cursor_updated"] is True
        assert isinstance(result["synced_at"], str)
        sync_service._update_synced_at.assert_called_once_with(result["synced_at"])


class TestLinearSyncServiceCreate:
    """Test create_issue_for_task method."""

    @pytest.mark.asyncio
    async def test_create_issue_calls_linear_mcp(self, sync_service, mock_mcp_manager):
        """create_issue_for_task calls Linear MCP create_issue."""
        mock_task = MagicMock()
        mock_task.title = "Feature: Add new thing"
        mock_task.description = "Adds a cool feature"
        mock_task.linear_team_id = "team-123"
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42

        sync_service.task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {
            "id": "lin-123",
            "title": "#42: Feature: Add new thing",
        }

        result = await sync_service.create_issue_for_task(task_id="test-task-id")

        mock_mcp_manager.call_tool.assert_called()
        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["title"] == (
            "#42: Feature: Add new thing"
        )
        assert result["gobby_ref"] == "#42"
        assert result["gobby_task_id"] == "test-task-id"
        assert result["linear_issue_id"] == "lin-123"

    @pytest.mark.asyncio
    async def test_create_issue_includes_linear_project(self, mock_mcp_manager, mock_task_manager):
        """create_issue_for_task includes team and project binding."""
        mock_task = MagicMock()
        mock_task.title = "Feature"
        mock_task.description = "Description"
        mock_task.linear_team_id = None
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42
        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {"id": "lin-456"}

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
            linear_project_id="lin-proj",
        )

        result = await service.create_issue_for_task(task_id="test-task-id")

        call = mock_mcp_manager.call_tool.call_args
        assert call.kwargs["arguments"]["teamId"] == "team-123"
        assert call.kwargs["arguments"]["projectId"] == "lin-proj"
        assert call.kwargs["arguments"]["title"] == "#42: Feature"
        assert result["gobby_ref"] == "#42"
        assert result["linear_project_id"] == "lin-proj"

    @pytest.mark.asyncio
    async def test_create_issue_creates_same_named_project_when_missing(
        self, mock_mcp_manager, mock_task_manager
    ):
        """create_issue_for_task creates and stores a same-named Linear project."""
        mock_task = MagicMock()
        mock_task.title = "Feature"
        mock_task.description = "Description"
        mock_task.linear_team_id = None
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42
        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {"id": "lin-456"}

        project = MagicMock()
        project.linear_project_id = None
        project.name = "gobby"
        project.repo_path = None
        project_manager = MagicMock()
        project_manager.get.return_value = project

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
            project_manager=project_manager,
        )
        service.ensure_linear_project = AsyncMock(
            return_value=({"id": "lin-proj", "name": "gobby"}, True)
        )

        await service.create_issue_for_task(task_id="test-task-id")

        service.ensure_linear_project.assert_awaited_once_with("team-123", "gobby")
        assert service.ensure_linear_project.await_count == 1
        assert service.ensure_linear_project.await_args is not None
        project_manager.update.assert_called_with(
            "test-project",
            linear_team_id="team-123",
            linear_project_id="lin-proj",
        )
        assert project_manager.update.call_count >= 1
        assert project_manager.update.call_args is not None
        assert service.linear_project_id == "lin-proj"

    @pytest.mark.asyncio
    async def test_create_issue_updates_task_linear_id(
        self, sync_service, mock_mcp_manager, mock_task_manager
    ):
        """create_issue_for_task updates task with linear_issue_id."""
        mock_task = MagicMock()
        mock_task.title = "Feature"
        mock_task.description = "Description"
        mock_task.linear_team_id = None
        mock_task.id = "test-task-id"
        mock_task.priority = 2

        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {"id": "lin-456"}

        await sync_service.create_issue_for_task(task_id="test-task-id")

        mock_task_manager.update_task.assert_called()
        assert mock_task_manager.update_task.call_count >= 1
        assert mock_task_manager.update_task.call_args is not None

    @pytest.mark.asyncio
    async def test_create_issue_raises_when_no_team_id(self, mock_mcp_manager, mock_task_manager):
        """create_issue_for_task raises ValueError when no team_id available."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}

        mock_task = MagicMock()
        mock_task.linear_team_id = None
        mock_task_manager.get_task.return_value = mock_task

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            # No linear_team_id
        )

        with pytest.raises(ValueError, match="team_id"):
            await service.create_issue_for_task(task_id="test-task")

    @pytest.mark.asyncio
    async def test_create_missing_issues_filters_closed_tasks(
        self, sync_service, mock_task_manager
    ):
        """create_missing_issues only selects unlinked non-closed tasks."""
        mock_task_manager.db.fetchall.return_value = []

        await sync_service.create_missing_issues()

        sql = mock_task_manager.db.fetchall.call_args.args[0]
        assert "linear_issue_id IS NULL" in sql
        assert "closed_at IS NULL" in sql

    @pytest.mark.asyncio
    async def test_sync_active_forward_creates_missing_and_pushes_active(
        self, sync_service, mock_task_manager
    ):
        """sync_active_forward creates missing active issues and skips pull."""
        sync_service.create_missing_issues = AsyncMock(return_value=[{"id": "lin-1"}])
        sync_service.push_active_tasks = AsyncMock(
            return_value={"pushed": 2, "skipped": 0, "errors": 0}
        )
        sync_service.pull_linear_updates = AsyncMock()
        sync_service._update_synced_at = MagicMock()

        result = await sync_service.sync_active_forward(team_id="team-123")

        assert result["mode"] == "forward_active"
        assert result["created_count"] == 1
        assert result["push"]["pushed"] == 2
        sync_service.create_missing_issues.assert_awaited_once_with(team_id="team-123")
        sync_service.push_active_tasks.assert_awaited_once_with()
        sync_service.pull_linear_updates.assert_not_called()
        sync_service._update_synced_at.assert_called_once()

    @pytest.mark.asyncio
    async def test_push_active_tasks_filters_closed_tasks(self, sync_service, mock_task_manager):
        """push_active_tasks only pushes linked non-closed tasks."""
        mock_task_manager.db.fetchall.return_value = [{"id": "task-1"}, {"id": "task-2"}]
        sync_service.sync_task_to_linear = AsyncMock()

        result = await sync_service.push_active_tasks()

        sql = mock_task_manager.db.fetchall.call_args.args[0]
        assert "linear_issue_id IS NOT NULL" in sql
        assert "closed_at IS NULL" in sql
        assert result == {"pushed": 2, "skipped": 0, "errors": 0}
        assert sync_service.sync_task_to_linear.await_count == 2


class TestStateMapping:
    """Test state mapping functions."""

    def test_map_gobby_state_to_linear_ready(self, sync_service) -> None:
        """map_gobby_state_to_linear converts ready to Todo."""
        assert sync_service.map_gobby_state_to_linear("ready") == "Todo"

    def test_map_gobby_state_to_linear_in_progress(self, sync_service) -> None:
        """map_gobby_state_to_linear converts in_progress to In Progress."""
        assert sync_service.map_gobby_state_to_linear("in_progress") == "In Progress"

    def test_map_gobby_state_to_linear_closed(self, sync_service) -> None:
        """map_gobby_state_to_linear converts closed to Done."""
        assert sync_service.map_gobby_state_to_linear("closed") == "Done"

    def test_map_gobby_state_to_linear_unknown(self, sync_service) -> None:
        """map_gobby_state_to_linear defaults to Todo for unknown state."""
        assert sync_service.map_gobby_state_to_linear("unknown") == "Todo"

    def test_map_linear_state_to_gobby_todo(self, sync_service) -> None:
        """map_linear_state_to_gobby converts Todo to ready."""
        assert sync_service.map_linear_state_to_gobby("Todo") == "ready"

    def test_map_linear_state_to_gobby_in_progress(self, sync_service) -> None:
        """map_linear_state_to_gobby converts In Progress to in_progress."""
        assert sync_service.map_linear_state_to_gobby("In Progress") == "in_progress"

    def test_map_linear_state_to_gobby_done(self, sync_service) -> None:
        """map_linear_state_to_gobby converts Done to closed."""
        assert sync_service.map_linear_state_to_gobby("Done") == "closed"

    def test_map_linear_state_to_gobby_unknown(self, sync_service) -> None:
        """map_linear_state_to_gobby defaults to ready for unknown state."""
        assert sync_service.map_linear_state_to_gobby("Unknown State") == "ready"


class TestLinearSyncIntegration:
    """Integration tests for full LinearSyncService workflows."""

    @pytest.mark.asyncio
    async def test_import_and_sync_workflow(self, mock_mcp_manager, mock_task_manager):
        """Test full workflow: import issues, then sync back."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}

        mock_mcp_manager.call_tool.side_effect = [
            # list_issues response
            {
                "issues": [
                    {
                        "id": "lin-42",
                        "title": "Original Title",
                        "description": "Original description",
                        "state": {"name": "Todo"},
                    }
                ]
            },
            # update_issue response
            {"id": "lin-42", "title": "Updated Title"},
        ]

        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.linear_issue_id = "lin-42"
        mock_task.linear_team_id = "team-123"
        mock_task.title = "Updated Title"
        mock_task.description = "Updated description"
        _set_task_state(mock_task, "in_progress")
        mock_task.priority = 2
        mock_task.to_dict.return_value = {"id": "gt-test123", "title": "Updated Title"}
        mock_task_manager.create_task.return_value = mock_task
        mock_task_manager.get_task.return_value = mock_task

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
        )

        imported = await service.import_linear_issues()
        assert len(imported) == 1

        result = await service.sync_task_to_linear(task_id="gt-test123")
        assert result is not None
        update_call = mock_mcp_manager.call_tool.call_args_list[-1]
        assert "stateId" not in update_call.kwargs["arguments"]

    @pytest.mark.asyncio
    async def test_handles_empty_issue_list(self, mock_mcp_manager, mock_task_manager):
        """Test handling of team with no issues."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.return_value = {"issues": []}

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
        )

        result = await service.import_linear_issues()
        assert result == []
        mock_task_manager.create_task.assert_not_called()


class TestLinearSyncExceptions:
    """Test custom exceptions and error handling."""

    def test_linear_sync_error_base_exception(self) -> None:
        """LinearSyncError is a base exception for sync errors."""
        from gobby.sync.linear import LinearSyncError

        error = LinearSyncError("Something went wrong")
        assert str(error) == "Something went wrong"
        assert isinstance(error, Exception)

    def test_linear_rate_limit_error(self) -> None:
        """LinearRateLimitError includes rate limit reset time."""
        from gobby.sync.linear import LinearRateLimitError

        error = LinearRateLimitError("Rate limited", reset_at=1234567890)
        assert "Rate limited" in str(error)
        assert error.reset_at == 1234567890

    def test_linear_not_found_error(self) -> None:
        """LinearNotFoundError indicates missing resource."""
        from gobby.sync.linear import LinearNotFoundError

        error = LinearNotFoundError(
            "Issue lin-42 not found", resource="issue", resource_id="lin-42"
        )
        assert "Issue lin-42 not found" in str(error)
        assert error.resource == "issue"
        assert error.resource_id == "lin-42"


class TestLinearSyncErrorHandling:
    """Test error handling in sync operations."""

    @pytest.mark.asyncio
    async def test_sync_validates_response_structure(self, mock_mcp_manager, mock_task_manager):
        """sync_task_to_linear validates response before processing."""
        from gobby.sync.linear import LinearSyncError

        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.return_value = None

        mock_task = MagicMock()
        mock_task.linear_issue_id = "lin-42"
        mock_task.linear_team_id = "team-123"
        mock_task.title = "Test"
        mock_task.description = "Test desc"
        _set_task_state(mock_task, "ready")
        mock_task.priority = 2
        mock_task_manager.get_task.return_value = mock_task

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )

        with pytest.raises((LinearSyncError, TypeError, AttributeError)):
            await service.sync_task_to_linear(task_id="test-task")

    @pytest.mark.asyncio
    async def test_error_recovery_network_failure(self, mock_mcp_manager, mock_task_manager):
        """Test error handling when network fails."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.side_effect = Exception("Network error")

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
        )

        with pytest.raises(Exception, match="Network error"):
            await service.import_linear_issues()

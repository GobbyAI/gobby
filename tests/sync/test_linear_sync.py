"""Tests for LinearSyncService class.

Tests verify the sync service correctly orchestrates between gobby tasks
and Linear via the official Linear MCP server.
"""

import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from gobby.integrations.linear_graphql import LinearGraphQLClient, LinearGraphQLError
from gobby.mcp_proxy.models import MCPError
from gobby.storage.cron_models import CronJob
from gobby.storage.secrets import SecretDecryptionError
from gobby.sync import linear as linear_module
from gobby.sync.linear import LinearSyncError, LinearSyncService
from gobby.sync.linear_support import (
    _extract_records,
    _linear_fetch_failure_limiter,
    is_transient_linear_fetch_error,
    linear_issue_title,
)
from gobby.sync.linear_task_ops import (
    _gobby_priority_to_linear,
    _linear_priority_to_gobby,
)

pytestmark = pytest.mark.unit

_TEST_MONKEYPATCH = pytest.MonkeyPatch()


@pytest.fixture(autouse=True)
def _restore_test_replacements() -> Iterator[None]:
    yield
    _TEST_MONKEYPATCH.undo()


@pytest.mark.parametrize(
    ("gobby_priority", "linear_priority"),
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)],
)
def test_gobby_priority_to_linear(gobby_priority: int, linear_priority: int) -> None:
    assert _gobby_priority_to_linear(gobby_priority) == linear_priority


@pytest.mark.parametrize(
    ("linear_priority", "gobby_priority"),
    [(0, 4), (1, 0), (2, 1), (3, 2), (4, 3)],
)
def test_linear_priority_to_gobby(linear_priority: int, gobby_priority: int) -> None:
    assert _linear_priority_to_gobby(linear_priority) == gobby_priority


def test_unknown_priorities_use_unprioritized_fallbacks() -> None:
    assert _gobby_priority_to_linear(99) == 0
    assert _linear_priority_to_gobby(99) == 4
    assert _linear_priority_to_gobby(None) == 4


@pytest.fixture(autouse=True)
def reset_linear_fetch_failure_limiter() -> Iterator[None]:
    _linear_fetch_failure_limiter.reset()
    yield
    _linear_fetch_failure_limiter.reset()


def test_extract_records_recurses_into_nested_nodes_wrapper() -> None:
    assert _extract_records({"data": {"issues": {"nodes": [{"id": "lin-1"}]}}}) == [{"id": "lin-1"}]


def test_extract_records_rejects_plain_record_dict() -> None:
    with pytest.raises(LinearSyncError, match="expected collection wrapper"):
        _extract_records({"id": "lin-1", "title": "Plain record"})


def test_linear_issue_title_requires_exact_ref_prefix() -> None:
    task = MagicMock(seq_num=42, title="#421: Different task")

    assert linear_issue_title(task) == "#42: #421: Different task"


def test_linear_issue_title_canonicalizes_existing_ref_spacing() -> None:
    task = MagicMock(seq_num=42, title="#42:Already prefixed")

    assert linear_issue_title(task) == "#42: Already prefixed"


def test_transient_linear_fetch_error_accepts_wrapped_network_failure() -> None:
    error = _wrapped_graphql_error(
        httpx.ConnectError("network unavailable", request=_linear_graphql_request())
    )

    assert is_transient_linear_fetch_error(error) is True


def test_transient_linear_fetch_error_accepts_wrapped_retryable_status() -> None:
    error = _wrapped_graphql_error(_http_status_error(503))

    assert is_transient_linear_fetch_error(error) is True


def test_transient_linear_fetch_error_rejects_permanent_graphql_failure() -> None:
    assert is_transient_linear_fetch_error(LinearGraphQLError("Invalid query")) is False
    assert is_transient_linear_fetch_error(_wrapped_graphql_error(_http_status_error(401))) is False


def _set_task_state(task: MagicMock, state: str) -> None:
    task.closed_at = None
    task.escalated_at = None
    task.is_escalated = False
    task.current_stage = {"state": state}


def _replace_for_test(target: object, name: str, replacement: object) -> None:
    """Replace a concrete service attribute with an explicit test double."""
    _TEST_MONKEYPATCH.setattr(target, name, replacement)


def _cron_job() -> CronJob:
    return CronJob(
        id="cj-linear",
        project_id="test-project-id",
        name="Linear Sync",
        schedule_type="cron",
        action_type="handler",
        action_config={"handler": "linear.sync"},
        created_at=datetime(2026, 2, 10, tzinfo=UTC),
        updated_at=datetime(2026, 2, 10, tzinfo=UTC),
    )


def _linear_graphql_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.linear.app/graphql")


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = _linear_graphql_request()
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def _wrapped_graphql_error(cause: BaseException) -> LinearGraphQLError:
    try:
        raise LinearGraphQLError("Linear GraphQL request failed after 3 attempts.") from cause
    except LinearGraphQLError as exc:
        return exc


def _configure_graphql_pull_result(
    sync_service: LinearSyncService, side_effect: object
) -> MagicMock:
    client = MagicMock()
    client.list_issues = AsyncMock(side_effect=side_effect)
    _replace_for_test(sync_service, "_linear_mcp_has_tool", MagicMock(return_value=False))
    _replace_for_test(sync_service, "_get_graphql_client", AsyncMock(return_value=client))
    return client


@pytest.fixture
def mock_mcp_manager() -> MagicMock:
    """Create a mock MCPClientManager."""
    manager = MagicMock()
    manager.has_server = MagicMock(return_value=True)
    manager.health = {"linear": MagicMock(state="connected")}
    manager.call_tool = AsyncMock()
    return manager


@pytest.fixture
def mock_task_manager() -> MagicMock:
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
def sync_service(
    mock_mcp_manager: MagicMock,
    mock_task_manager: MagicMock,
) -> LinearSyncService:
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


@pytest.mark.asyncio
async def test_linear_sync_handler_raises_on_partial_errors(
    mock_mcp_manager: MagicMock,
    mock_task_manager: MagicMock,
) -> None:
    """Nonzero pull/push error counts fail the cron handler."""
    service = MagicMock()
    service.is_available.return_value = True
    sync_all_mock = AsyncMock(
        return_value={
            "pull": {"updated": 0, "skipped": 0, "errors": 1, "deferred": 0},
            "push": {"pushed": 0, "errors": 2, "deferred": 0},
        }
    )
    _replace_for_test(service, "sync_all", sync_all_mock)
    handler = linear_module.create_linear_sync_handler(
        mock_mcp_manager,
        mock_task_manager,
        project_id="test-project-id",
        team_id="team-123",
    )

    with patch.object(linear_module, "LinearSyncService", return_value=service):
        with pytest.raises(RuntimeError, match="pull_errors=1, push_errors=2"):
            await handler(_cron_job())


@pytest.mark.asyncio
async def test_linear_sync_handler_succeeds_after_skipped_pull_conflict(
    mock_mcp_manager: MagicMock,
    mock_task_manager: MagicMock,
) -> None:
    """A skipped newer-local conflict proceeds through a successful cron sync."""
    service = MagicMock()
    service.is_available.return_value = True
    sync_all_mock = AsyncMock(
        return_value={
            "pull": {"updated": 0, "skipped": 1, "errors": 0, "deferred": 0},
            "push": {"pushed": 1, "skipped": 0, "errors": 0, "deferred": 0},
        }
    )
    _replace_for_test(service, "sync_all", sync_all_mock)
    handler = linear_module.create_linear_sync_handler(
        mock_mcp_manager,
        mock_task_manager,
        project_id="test-project-id",
        team_id="team-123",
    )

    with patch.object(linear_module, "LinearSyncService", return_value=service):
        result = await handler(_cron_job())

    assert result == (
        "Linear sync complete: pulled 0 (skipped 1, errors 0, deferred 0), "
        "pushed 1 (errors 0, deferred 0)"
    )
    sync_all_mock.assert_awaited_once_with(team_id="team-123")


@pytest.mark.asyncio
async def test_linear_sync_handler_reports_deferred_without_raising(
    mock_mcp_manager: MagicMock,
    mock_task_manager: MagicMock,
) -> None:
    """Deferred-only sync results do not fail cron."""
    service = MagicMock()
    service.is_available.return_value = True
    sync_all_mock = AsyncMock(
        return_value={
            "pull": {"updated": 0, "skipped": 0, "errors": 0, "deferred": 83},
            "push": {"pushed": 0, "errors": 0, "deferred": 0},
        }
    )
    _replace_for_test(service, "sync_all", sync_all_mock)
    handler = linear_module.create_linear_sync_handler(
        mock_mcp_manager,
        mock_task_manager,
        project_id="test-project-id",
        team_id="team-123",
    )

    with patch.object(linear_module, "LinearSyncService", return_value=service):
        result = await handler(_cron_job())

    assert "Linear sync deferred" in result
    assert "deferred 83" in result


@pytest.mark.asyncio
async def test_linear_sync_handler_reraises_sync_exceptions(
    mock_mcp_manager: MagicMock,
    mock_task_manager: MagicMock,
) -> None:
    """sync_all exceptions propagate so cron backoff engages."""
    service = MagicMock()
    service.is_available.return_value = True
    sync_all_mock = AsyncMock(side_effect=RuntimeError("linear unavailable"))
    _replace_for_test(service, "sync_all", sync_all_mock)
    handler = linear_module.create_linear_sync_handler(
        mock_mcp_manager,
        mock_task_manager,
        project_id="test-project-id",
        team_id="team-123",
    )

    with patch.object(linear_module, "LinearSyncService", return_value=service):
        with pytest.raises(RuntimeError, match="linear unavailable"):
            await handler(_cron_job())


class TestLinearSyncServiceInit:
    """Test LinearSyncService initialization."""

    def test_init_with_dependencies(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        """LinearSyncService initializes with mcp_manager and task_manager."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.mcp_manager is mock_mcp_manager
        assert service.task_manager is mock_task_manager
        assert service.project_id == "test-project"

    def test_init_creates_linear_integration(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        """LinearSyncService creates LinearIntegration for availability checks."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert hasattr(service, "linear")
        from gobby.integrations.linear import LinearIntegration

        assert isinstance(service.linear, LinearIntegration)

    def test_init_default_team_id_is_none(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        """Default linear_team_id is None if not specified."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
        )
        assert service.linear_team_id is None

    def test_init_with_team_id(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        """LinearSyncService accepts linear_team_id parameter."""
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-abc",
        )
        assert service.linear_team_id == "team-abc"

    def test_init_with_project_id(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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

    @pytest.mark.parametrize(
        ("mcp_available", "graphql_configured", "expected"),
        [
            pytest.param(True, False, True, id="mcp-only"),
            pytest.param(False, True, True, id="graphql-only"),
            pytest.param(True, True, True, id="both-configured"),
            pytest.param(False, False, False, id="neither-configured"),
        ],
    )
    def test_is_available_accepts_mcp_or_graphql_configuration(
        self,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
        mcp_available: bool,
        graphql_configured: bool,
        expected: bool,
    ) -> None:
        mock_mcp_manager.has_server.return_value = mcp_available
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")} if mcp_available else {}
        graphql_client = MagicMock(spec=LinearGraphQLClient) if graphql_configured else None

        with patch(
            "gobby.sync.linear.LinearGraphQLClient.from_database",
            return_value=graphql_client,
        ):
            service = LinearSyncService(
                mcp_manager=mock_mcp_manager,
                task_manager=mock_task_manager,
                project_id="test-project",
            )

            assert service.is_available() is expected

    def test_unavailable_reason_reports_missing_graphql_configuration(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        mock_mcp_manager.has_server.return_value = False

        with patch("gobby.sync.linear.LinearGraphQLClient.from_database", return_value=None):
            service = LinearSyncService(
                mcp_manager=mock_mcp_manager,
                task_manager=mock_task_manager,
                project_id="test-project",
            )

            reason = service.get_unavailable_reason()

        assert reason is not None
        assert "Linear MCP server 'linear' is not configured" in reason
        assert "Linear GraphQL API key is not configured" in reason

    def test_invalid_graphql_configuration_is_unavailable(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        mock_mcp_manager.has_server.return_value = False

        with patch(
            "gobby.sync.linear.LinearGraphQLClient.from_database",
            side_effect=SecretDecryptionError("linear_api_key"),
        ):
            service = LinearSyncService(
                mcp_manager=mock_mcp_manager,
                task_manager=mock_task_manager,
                project_id="test-project",
            )

            assert service.is_available() is False
            reason = service.get_unavailable_reason()

        assert reason is not None
        assert "cannot be decrypted" in reason


class TestLinearSyncServiceImport:
    """Test import_linear_issues method."""

    @pytest.mark.asyncio
    async def test_import_issues_calls_linear_mcp(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        """import_linear_issues calls Linear MCP list_issues tool."""
        mock_mcp_manager.call_tool.return_value = {"issues": []}

        await sync_service.import_linear_issues(team_id="team-123")

        mock_mcp_manager.call_tool.assert_called()
        calls = mock_mcp_manager.call_tool.call_args_list
        assert any("linear" in str(call) for call in calls)

    @pytest.mark.asyncio
    async def test_import_issues_rejects_invalid_mcp_payload(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        """import_linear_issues rejects malformed list_issues payloads."""
        mock_mcp_manager.call_tool.return_value = {"issues": "not-a-list"}

        with pytest.raises(LinearSyncError, match="Invalid Linear MCP response"):
            await sync_service.import_linear_issues(team_id="team-123")

    @pytest.mark.asyncio
    async def test_import_issues_creates_tasks(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        """import_linear_issues creates gobby tasks from Linear issues."""
        mock_mcp_manager.call_tool.return_value = CallToolResult(
            content=[],
            structured_content={
                "issues": [
                    {"id": "issue-1", "title": "Issue 1", "description": "Description 1"},
                    {"id": "issue-2", "title": "Issue 2", "description": "Description 2"},
                ]
            },
            is_error=False,
        )

        await sync_service.import_linear_issues()

        assert mock_task_manager.create_task.call_count >= 2
        assert all(
            call.kwargs["priority"] == 4 for call in mock_task_manager.create_task.call_args_list
        )

    @pytest.mark.asyncio
    async def test_import_issues_links_linear_fields(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
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
    async def test_import_done_issue_creates_closed_task(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "lin-done",
                    "title": "Finished feature",
                    "state": {"name": "Done"},
                    "updatedAt": "2026-02-11T12:34:56Z",
                }
            ]
        }
        created = MagicMock()
        created.id = "task-done"
        created.to_dict.return_value = {"id": "task-done"}
        closed = MagicMock()
        closed.to_dict.return_value = {"id": "task-done", "closed_reason": "linear_sync"}
        mock_task_manager.create_task.return_value = created
        mock_task_manager.reconcile_task_state.return_value = closed

        imported = await sync_service.import_linear_issues()

        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "task-done",
            closed_at="2026-02-11T12:34:56+00:00",
            closed_reason="linear_sync",
            closed_in_session_id=None,
            closed_commit_sha=None,
            escalated_at=None,
            escalation_reason=None,
        )
        assert imported == [{"id": "task-done", "closed_reason": "linear_sync"}]

    @pytest.mark.asyncio
    async def test_import_canceled_issue_reconciles_existing_task(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "lin-canceled",
                    "title": "Canceled feature",
                    "state": {"name": "Canceled"},
                    "updatedAt": "2026-02-12T09:30:00Z",
                }
            ]
        }
        existing = MagicMock()
        existing.to_dict.return_value = {"id": "task-canceled"}
        mock_task_manager.db.fetchone.return_value = {"id": "task-canceled"}
        mock_task_manager.get_task.return_value = existing

        imported = await sync_service.import_linear_issues()

        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "task-canceled",
            title="Canceled feature",
            description="",
            priority=4,
            closed_at=None,
            closed_reason=None,
            closed_in_session_id=None,
            closed_commit_sha=None,
            escalated_at="2026-02-12T09:30:00+00:00",
            escalation_reason="Linear state: Canceled",
        )
        assert imported == [{"id": "task-canceled"}]

    @pytest.mark.asyncio
    async def test_import_preserves_newer_local_fields(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "lin-stale",
                    "title": "Stale remote title",
                    "description": "Stale remote body",
                    "updatedAt": "2026-02-11T12:00:00Z",
                }
            ]
        }
        mock_task_manager.db.fetchone.return_value = {
            "id": "task-stale",
            "updated_at": "2026-02-12T12:00:00Z",
        }
        existing = MagicMock()
        existing.to_dict.return_value = {
            "id": "task-stale",
            "title": "Newer local title",
            "description": "Newer local body",
        }
        mock_task_manager.get_task.return_value = existing

        imported = await sync_service.import_linear_issues()

        mock_task_manager.reconcile_task_state.assert_not_called()
        assert imported == [existing.to_dict.return_value]

    @pytest.mark.asyncio
    async def test_import_applies_newer_remote_fields(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "lin-newer",
                    "title": "Newer remote title",
                    "description": "Newer remote body",
                    "updatedAt": "2026-02-12T12:00:00Z",
                }
            ]
        }
        mock_task_manager.db.fetchone.return_value = {
            "id": "task-newer",
            "updated_at": "2026-02-11T12:00:00Z",
        }
        existing = MagicMock()
        existing.to_dict.return_value = {"id": "task-newer"}
        mock_task_manager.get_task.return_value = existing

        imported = await sync_service.import_linear_issues()

        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "task-newer",
            title="Newer remote title",
            description="Newer remote body",
            priority=4,
        )
        assert imported == [{"id": "task-newer"}]

    @pytest.mark.asyncio
    async def test_import_links_existing_task_by_gobby_ref_title(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
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
            validation_criteria=(
                "The acceptance conditions recorded in Linear issue lin-42 are implemented, "
                "and the resulting behavior is verified by authoritative current-state "
                "evidence."
            ),
        )
        assert mock_task_manager.update_task.call_count >= 1
        assert mock_task_manager.update_task.call_args is not None
        mock_task_manager.reconcile_task_state.assert_called_with(
            "task-42",
            title="Existing Feature",
            description="Test body",
            priority=4,
        )
        assert mock_task_manager.reconcile_task_state.call_count >= 1
        assert mock_task_manager.reconcile_task_state.call_args is not None
        mock_task_manager.create_task.assert_not_called()
        assert mock_task_manager.create_task.call_count == 0
        assert not mock_task_manager.create_task.called
        assert any(
            call.args
            == (
                "SELECT id, validation_criteria, updated_at FROM tasks "
                "WHERE project_id = %s AND seq_num = %s",
                ("test-project-id", 42),
            )
            for call in mock_task_manager.db.fetchone.call_args_list
        )

    @pytest.mark.asyncio
    async def test_import_issues_raises_when_unavailable(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
    async def test_import_issues_raises_when_no_team_id(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
        assert call.kwargs["arguments"]["limit"] == 100

    async def test_import_issues_requires_linear_project_binding(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
        )

        with pytest.raises(ValueError, match="gobby linear setup --bootstrap"):
            await service.import_linear_issues()

        mock_mcp_manager.call_tool.assert_not_called()

    async def test_import_issues_allows_explicit_team_wide_override(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        mock_mcp_manager.call_tool.return_value = {"issues": []}
        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
        )

        await service.import_linear_issues(allow_team_wide=True)

        call = mock_mcp_manager.call_tool.call_args
        assert call.kwargs["arguments"]["teamId"] == "team-123"
        assert "projectId" not in call.kwargs["arguments"]

    @pytest.mark.asyncio
    async def test_linear_mcp_issue_listing_fetches_every_cursor_page(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        first_page = [{"id": f"issue-{index}"} for index in range(100)]
        second_page = [{"id": f"issue-{index}"} for index in range(100, 125)]
        mock_mcp_manager.call_tool.side_effect = [
            {"issues": first_page, "hasNextPage": True, "cursor": "cursor-100"},
            {"issues": second_page, "hasNextPage": False, "cursor": None},
        ]

        issues = await sync_service._list_issues_via_mcp("team-123")

        assert [issue["id"] for issue in issues] == [f"issue-{index}" for index in range(125)]
        calls = mock_mcp_manager.call_tool.await_args_list
        assert calls[0].kwargs["arguments"] == {
            "teamId": "team-123",
            "limit": 100,
            "projectId": "lin-proj",
        }
        assert calls[1].kwargs["arguments"] == {
            "teamId": "team-123",
            "limit": 100,
            "projectId": "lin-proj",
            "cursor": "cursor-100",
        }

    @pytest.mark.asyncio
    async def test_sync_all_preserves_cursor_when_linked_issue_is_missing(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_task_manager.db.fetchall.return_value = [
            {"id": "task-1", "linear_issue_id": "missing-issue"}
        ]
        mock_mcp_manager.call_tool.return_value = {"issues": []}
        get_project_synced_at_mock = MagicMock(return_value="old-cursor")
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)
        push_dirty_tasks_mock = AsyncMock()
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)

        result = await sync_service.sync_all(team_id="team-123")

        assert result["pull"] == {
            "updated": 0,
            "skipped": 0,
            "errors": 1,
            "deferred": 0,
        }
        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        push_dirty_tasks_mock.assert_not_awaited()
        update_synced_at_mock.assert_not_called()


class TestLinearSyncServiceSync:
    """Test sync_task_to_linear method."""

    @pytest.mark.asyncio
    async def test_sync_task_calls_linear_mcp(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        """sync_task_to_linear calls Linear MCP to update issue."""
        mock_task = MagicMock()
        mock_task.linear_issue_id = "lin-42"
        mock_task.linear_team_id = "team-123"
        mock_task.title = "Updated Title"
        mock_task.description = "Updated description"
        _set_task_state(mock_task, "in_progress")
        mock_task.priority = 2

        _replace_for_test(sync_service.task_manager.get_task, "return_value", mock_task)
        mock_mcp_manager.call_tool.return_value = {"success": True}

        await sync_service.sync_task_to_linear(task_id="test-task-id")

        mock_mcp_manager.call_tool.assert_called()
        assert mock_mcp_manager.call_tool.call_count >= 1
        assert mock_mcp_manager.call_tool.call_args is not None
        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["priority"] == 3

    @pytest.mark.asyncio
    async def test_sync_task_graphql_resolves_linear_state_id(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
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
        get_graphql_client_mock = AsyncMock(return_value=client)
        _replace_for_test(sync_service, "_get_graphql_client", get_graphql_client_mock)
        _replace_for_test(sync_service.task_manager.get_task, "return_value", mock_task)

        await sync_service.sync_task_to_linear(task_id="test-task-id")

        client.list_team_states.assert_awaited_once_with("team-123")
        assert client.list_team_states.await_count == 1
        assert client.list_team_states.await_args is not None
        client.update_issue.assert_awaited_once_with(
            issue_id="lin-42",
            title="#42: Updated Title",
            description="Updated description",
            priority=3,
            state_id="state-progress",
        )
        assert client.update_issue.await_count == 1
        assert client.update_issue.await_args is not None
        mock_mcp_manager.call_tool.assert_not_called()
        assert mock_mcp_manager.call_tool.call_count == 0
        assert not mock_mcp_manager.call_tool.called

    async def test_sync_task_graphql_preserves_active_state_when_escalated(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        """Waiting escalations remain in their active Linear workflow state."""
        mock_task = MagicMock()
        mock_task.id = "test-task-id"
        mock_task.seq_num = 42
        mock_task.linear_issue_id = "lin-42"
        mock_task.linear_team_id = "team-123"
        mock_task.title = "Waiting for organic data"
        mock_task.description = "Collection remains active"
        _set_task_state(mock_task, "in_progress")
        mock_task.escalated_at = datetime.now(UTC)
        mock_task.is_escalated = True
        mock_task.escalation_reason = "Waiting for organic data"
        mock_task.priority = 2

        client = MagicMock()
        client.list_team_states = AsyncMock(
            return_value=[
                {"id": "state-canceled", "name": "Canceled"},
                {"id": "state-progress", "name": "In Progress"},
            ]
        )
        client.update_issue = AsyncMock(return_value={"id": "lin-42"})
        get_graphql_client_mock = AsyncMock(return_value=client)
        _replace_for_test(sync_service, "_get_graphql_client", get_graphql_client_mock)
        _replace_for_test(sync_service.task_manager.get_task, "return_value", mock_task)

        result = await sync_service.sync_task_to_linear(task_id="test-task-id")

        assert result == {"id": "lin-42"}
        client.list_team_states.assert_awaited_once_with("team-123")
        client.update_issue.assert_awaited_once_with(
            issue_id="lin-42",
            title="#42: Waiting for organic data",
            description="Collection remains active",
            priority=3,
            state_id="state-progress",
        )
        assert client.update_issue.await_count == 1
        assert client.update_issue.await_args is not None
        assert mock_task.is_escalated is True
        assert mock_task.escalation_reason == "Waiting for organic data"
        mock_mcp_manager.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_task_raises_when_no_issue_id(
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """sync_task_to_linear raises ValueError when task has no linear_issue_id."""
        mock_task = MagicMock()
        mock_task.linear_issue_id = None

        mock_task_manager.get_task.return_value = mock_task

        with pytest.raises(ValueError, match="issue"):
            await sync_service.sync_task_to_linear(task_id="test-task-id")

    @pytest.mark.asyncio
    async def test_pull_updates_scopes_to_linear_project(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
    async def test_pull_updates_reconciles_closed_lifecycle_fields(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        """Closed Linear issues clear escalation fields and set explicit closure metadata."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": "task-1", "linear_issue_id": "issue-1"}
        ]
        linear_mcp_has_tool_mock = MagicMock(return_value=True)
        _replace_for_test(sync_service, "_linear_mcp_has_tool", linear_mcp_has_tool_mock)
        get_project_synced_at_mock = MagicMock(return_value=None)
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "issue-1",
                    "title": "Finished feature",
                    "description": "Ready to close",
                    "priority": 1,
                    "state": {"name": "Done"},
                    "updatedAt": "2026-02-11T12:34:56Z",
                }
            ]
        }

        result = await sync_service.pull_linear_updates()

        assert result == {"updated": 1, "skipped": 0, "errors": 0, "deferred": 0}
        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "task-1",
            title="Finished feature",
            description="Ready to close",
            priority=0,
            closed_at="2026-02-11T12:34:56+00:00",
            closed_reason="linear_sync",
            closed_in_session_id=None,
            closed_commit_sha=None,
            escalated_at=None,
            escalation_reason=None,
        )

    @pytest.mark.asyncio
    async def test_pull_active_state_preserves_local_escalation(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        """Active Linear states do not clear internal waiting escalations."""
        mock_task_manager.db.fetchall.return_value = [
            {
                "id": "task-1",
                "linear_issue_id": "issue-1",
                "title": "Old checkpoint title",
                "description": "Collection remains active",
                "priority": 1,
                "updated_at": "2026-02-10T12:34:56Z",
                "closed_at": None,
                "closed_reason": None,
                "closed_in_session_id": None,
                "closed_commit_sha": None,
                "escalated_at": "2026-02-09T12:34:56+00:00",
                "escalation_reason": "Waiting for organic data",
            }
        ]
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "issue-1",
                    "title": "Updated checkpoint title",
                    "description": "Collection remains active",
                    "priority": 2,
                    "state": {"name": "In Progress"},
                    "updatedAt": "2026-02-11T12:34:56Z",
                }
            ]
        }

        result = await sync_service.pull_linear_updates()

        assert result == {"updated": 1, "skipped": 0, "errors": 0, "deferred": 0}
        mock_task_manager.reconcile_task_state.assert_called_once_with(
            "task-1",
            title="Updated checkpoint title",
            description="Collection remains active",
            priority=1,
            closed_at=None,
            closed_reason=None,
            closed_in_session_id=None,
            closed_commit_sha=None,
        )

    @pytest.mark.asyncio
    async def test_pull_updates_preserves_newer_local_changes(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_task_manager.db.fetchall.return_value = [
            {
                "id": "task-1",
                "linear_issue_id": "issue-1",
                "title": "New local title",
                "description": "Description",
                "priority": 2,
                "updated_at": datetime(2026, 2, 12, 12, 34, 56, tzinfo=UTC),
                "closed_at": None,
                "closed_reason": None,
                "closed_in_session_id": None,
                "closed_commit_sha": None,
                "escalated_at": None,
                "escalation_reason": None,
            }
        ]
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "issue-1",
                    "title": "Older Linear title",
                    "description": "Description",
                    "priority": 3,
                    "state": {"name": "Todo"},
                    "updatedAt": "2026-02-11T12:34:56Z",
                }
            ]
        }

        result = await sync_service.pull_linear_updates()

        assert result == {"updated": 0, "skipped": 1, "errors": 0, "deferred": 0}
        mock_task_manager.reconcile_task_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_updates_skips_unchanged_task_without_touching_updated_at(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        mock_task_manager.db.fetchall.return_value = [
            {
                "id": "task-1",
                "linear_issue_id": "issue-1",
                "title": "Same title",
                "description": None,
                "priority": 2,
                "updated_at": "2026-02-10T12:34:56Z",
                "closed_at": None,
                "closed_reason": None,
                "closed_in_session_id": None,
                "closed_commit_sha": None,
                "escalated_at": None,
                "escalation_reason": None,
            }
        ]
        mock_mcp_manager.call_tool.return_value = {
            "issues": [
                {
                    "id": "issue-1",
                    "title": "Same title",
                    "description": "",
                    "priority": 3,
                    "state": {"name": "Todo"},
                    "updatedAt": "2026-02-11T12:34:56Z",
                }
            ]
        }

        result = await sync_service.pull_linear_updates()

        assert result == {"updated": 0, "skipped": 1, "errors": 0, "deferred": 0}
        mock_task_manager.reconcile_task_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_updates_rate_limits_repeated_linear_fetch_failures(
        self,
        sync_service: LinearSyncService,
        mock_task_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repeated identical fetch failures do not emit repeated error logs."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": "task-1", "linear_issue_id": "issue-1"}
        ]
        _configure_graphql_pull_result(
            sync_service,
            _wrapped_graphql_error(
                httpx.ConnectError("network unavailable", request=_linear_graphql_request())
            ),
        )
        caplog.set_level(logging.DEBUG, logger="gobby.sync.linear")

        first = await sync_service.pull_linear_updates()
        second = await sync_service.pull_linear_updates()

        warning_records = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and "Deferred Linear issue fetch" in record.getMessage()
        ]
        assert first["errors"] == 0
        assert first["deferred"] == 1
        assert second["errors"] == 0
        assert second["deferred"] == 1
        assert len(warning_records) == 1
        assert any(
            "Suppressing repeated Linear issue fetch failure #1" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_pull_updates_defers_transient_graphql_fetch_for_all_linked_tasks(
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """One transient list-fetch exhaustion defers all linked rows."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": f"task-{index}", "linear_issue_id": f"issue-{index}"} for index in range(83)
        ]
        client = _configure_graphql_pull_result(
            sync_service,
            _wrapped_graphql_error(
                httpx.ConnectError("network unavailable", request=_linear_graphql_request())
            ),
        )

        result = await sync_service.pull_linear_updates()

        assert result == {"updated": 0, "skipped": 0, "errors": 0, "deferred": 83}
        sync_service._linear_mcp_has_tool.assert_called_once_with("list_issues")  # type: ignore[attr-defined]
        sync_service._get_graphql_client.assert_awaited_once_with()  # type: ignore[attr-defined]
        client.list_issues.assert_awaited_once_with(team_id="team-123", project_id="lin-proj")
        mock_task_manager.reconcile_task_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_updates_counts_permanent_graphql_failure_as_errors(
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """Permanent GraphQL failures still count against linked rows."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": "task-1", "linear_issue_id": "issue-1"},
            {"id": "task-2", "linear_issue_id": "issue-2"},
        ]
        client = _configure_graphql_pull_result(sync_service, LinearGraphQLError("Invalid query"))

        result = await sync_service.pull_linear_updates()

        assert result == {"updated": 0, "skipped": 0, "errors": 2, "deferred": 0}
        sync_service._linear_mcp_has_tool.assert_called_once_with("list_issues")  # type: ignore[attr-defined]
        sync_service._get_graphql_client.assert_awaited_once_with()  # type: ignore[attr-defined]
        client.list_issues.assert_awaited_once_with(team_id="team-123", project_id="lin-proj")
        mock_task_manager.reconcile_task_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_updates_recovery_resets_linear_fetch_failure_limit(
        self,
        sync_service: LinearSyncService,
        mock_task_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Successful fetch logs recovery and lets the next failure surface again."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": "task-1", "linear_issue_id": "issue-1"}
        ]
        _configure_graphql_pull_result(
            sync_service,
            [
                _wrapped_graphql_error(
                    httpx.ConnectError("network unavailable", request=_linear_graphql_request())
                ),
                _wrapped_graphql_error(
                    httpx.ConnectError("network unavailable", request=_linear_graphql_request())
                ),
                [],
                _wrapped_graphql_error(
                    httpx.ConnectError("network unavailable", request=_linear_graphql_request())
                ),
            ],
        )
        caplog.set_level(logging.DEBUG, logger="gobby.sync.linear")

        await sync_service.pull_linear_updates()
        await sync_service.pull_linear_updates()
        recovered = await sync_service.pull_linear_updates()
        await sync_service.pull_linear_updates()

        warning_records = [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING
            and "Deferred Linear issue fetch" in record.getMessage()
        ]
        assert recovered["errors"] == 1
        assert recovered["deferred"] == 0
        assert recovered["skipped"] == 0
        assert len(warning_records) == 2
        assert any(
            "Linear issue fetch recovered after 1 suppressed repeat(s)" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_sync_all_does_not_update_cursor_when_pull_has_errors(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all preserves the cursor when Linear pull fails."""
        pull_linear_updates_mock = AsyncMock(
            return_value={"updated": 0, "skipped": 0, "errors": 2, "deferred": 0}
        )
        _replace_for_test(sync_service, "pull_linear_updates", pull_linear_updates_mock)
        push_dirty_tasks_mock = AsyncMock()
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)
        get_project_synced_at_mock = MagicMock(return_value="old-cursor")
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        result = await sync_service.sync_all(team_id="team-123")

        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        assert result["push"] == {"pushed": 0, "skipped": 0, "errors": 0, "deferred": 0}
        push_dirty_tasks_mock.assert_not_called()
        update_synced_at_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_preserves_cursor_after_incomplete_paginated_fetch(
        self,
        sync_service: LinearSyncService,
        mock_task_manager: MagicMock,
    ) -> None:
        """A failed later page cannot advance the project sync watermark."""
        mock_task_manager.db.fetchall.return_value = [
            {"id": f"task-{index}", "linear_issue_id": f"issue-{index}"} for index in range(125)
        ]
        _configure_graphql_pull_result(
            sync_service,
            LinearGraphQLError("Linear pagination page 2 was incomplete."),
        )
        get_project_synced_at_mock = MagicMock(return_value="old-cursor")
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)
        push_dirty_tasks_mock = AsyncMock()
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)

        result = await sync_service.sync_all(team_id="team-123")

        assert result["pull"] == {
            "updated": 0,
            "skipped": 0,
            "errors": 125,
            "deferred": 0,
        }
        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        push_dirty_tasks_mock.assert_not_awaited()
        update_synced_at_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_does_not_update_cursor_or_push_when_pull_is_deferred(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all preserves pull-first safety when Linear pull is deferred."""
        pull_linear_updates_mock = AsyncMock(
            return_value={"updated": 0, "skipped": 0, "errors": 0, "deferred": 83}
        )
        _replace_for_test(sync_service, "pull_linear_updates", pull_linear_updates_mock)
        push_dirty_tasks_mock = AsyncMock()
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)
        get_project_synced_at_mock = MagicMock(return_value="old-cursor")
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        result = await sync_service.sync_all(team_id="team-123")

        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        assert result["push"] == {"pushed": 0, "skipped": 0, "errors": 0, "deferred": 0}
        push_dirty_tasks_mock.assert_not_called()
        update_synced_at_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_does_not_update_cursor_when_push_has_errors(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all preserves the cursor when Linear push fails."""
        pull_linear_updates_mock = AsyncMock(
            return_value={"updated": 1, "skipped": 0, "errors": 0, "deferred": 0}
        )
        _replace_for_test(sync_service, "pull_linear_updates", pull_linear_updates_mock)
        push_dirty_tasks_mock = AsyncMock(
            return_value={"pushed": 0, "skipped": 0, "errors": 1, "deferred": 0}
        )
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)
        get_project_synced_at_mock = MagicMock(return_value="old-cursor")
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        result = await sync_service.sync_all(team_id="team-123")

        assert result["cursor_updated"] is False
        assert result["synced_at"] == "old-cursor"
        update_synced_at_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_updates_cursor_when_pull_and_push_succeed(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_all pushes after a skipped pull conflict and advances the cursor."""
        pull_linear_updates_mock = AsyncMock(
            return_value={"updated": 0, "skipped": 1, "errors": 0, "deferred": 0}
        )
        _replace_for_test(sync_service, "pull_linear_updates", pull_linear_updates_mock)
        push_dirty_tasks_mock = AsyncMock(
            return_value={"pushed": 1, "skipped": 0, "errors": 0, "deferred": 0}
        )
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)
        get_project_synced_at_mock = MagicMock(return_value="old-cursor")
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        result = await sync_service.sync_all(team_id="team-123")

        assert result["pull"] == {"updated": 0, "skipped": 1, "errors": 0, "deferred": 0}
        assert result["push"] == {"pushed": 1, "skipped": 0, "errors": 0, "deferred": 0}
        assert result["cursor_updated"] is True
        assert isinstance(result["synced_at"], str)
        push_dirty_tasks_mock.assert_awaited_once_with()
        update_synced_at_mock.assert_called_once_with(result["synced_at"])

    async def test_sync_all_next_run_sees_edit_created_during_sync(
        self, sync_service: LinearSyncService
    ) -> None:
        """The cursor precedes edits created inside the sync window."""
        sync_started_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        edit_created_at = datetime(2026, 7, 14, 12, 1, tzinfo=UTC)
        clock = {"now": sync_started_at}
        state: dict[str, datetime | str | None] = {"cursor": None, "edit": None}

        async def pull_updates(*, team_id: str | None = None) -> dict[str, int]:
            del team_id
            cursor = state["cursor"]
            edit = state["edit"]
            updated = int(
                isinstance(cursor, str)
                and isinstance(edit, datetime)
                and edit > datetime.fromisoformat(cursor)
            )
            return {"updated": updated, "skipped": 1 - updated, "errors": 0, "deferred": 0}

        async def push_tasks() -> dict[str, int]:
            if state["edit"] is None:
                clock["now"] = edit_created_at
                state["edit"] = edit_created_at
            return {"pushed": 0, "skipped": 1, "errors": 0, "deferred": 0}

        pull_linear_updates_mock = AsyncMock(side_effect=pull_updates)
        _replace_for_test(sync_service, "pull_linear_updates", pull_linear_updates_mock)
        push_dirty_tasks_mock = AsyncMock(side_effect=push_tasks)
        _replace_for_test(sync_service, "push_dirty_tasks", push_dirty_tasks_mock)
        get_project_synced_at_mock = MagicMock(side_effect=lambda: state["cursor"])
        _replace_for_test(sync_service, "_get_project_synced_at", get_project_synced_at_mock)
        update_synced_at_mock = MagicMock(
            side_effect=lambda synced_at: state.update(cursor=synced_at)
        )
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        with patch("gobby.sync.linear_task_ops.datetime") as mock_datetime:
            mock_datetime.now.side_effect = lambda timezone: clock["now"]
            first = await sync_service.sync_all(team_id="team-123")
            second = await sync_service.sync_all(team_id="team-123")

        assert first["synced_at"] == sync_started_at.isoformat()
        assert second["pull"]["updated"] == 1


class TestLinearSyncServiceCreate:
    """Test create_issue_for_task method."""

    @pytest.mark.asyncio
    async def test_create_issue_calls_linear_mcp(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        """create_issue_for_task calls Linear MCP create_issue."""
        mock_task = MagicMock()
        mock_task.title = "Feature: Add new thing"
        mock_task.description = "Adds a cool feature"
        mock_task.linear_team_id = "team-123"
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42

        _replace_for_test(sync_service.task_manager.get_task, "return_value", mock_task)
        mock_mcp_manager.call_tool.return_value = CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text='{"id":"lin-123","title":"#42: Feature: Add new thing"}',
                )
            ],
            is_error=False,
        )

        result = await sync_service.create_issue_for_task(task_id="test-task-id")

        mock_mcp_manager.call_tool.assert_called()
        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["title"] == (
            "#42: Feature: Add new thing"
        )
        assert mock_mcp_manager.call_tool.call_args.kwargs["arguments"]["priority"] == 3
        assert result["gobby_ref"] == "#42"
        assert result["gobby_task_id"] == "test-task-id"
        assert result["linear_issue_id"] == "lin-123"

    @pytest.mark.asyncio
    async def test_create_issue_includes_linear_project(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
        ensure_linear_project_mock = AsyncMock(
            return_value=({"id": "lin-proj", "name": "gobby"}, True)
        )
        _replace_for_test(service, "ensure_linear_project", ensure_linear_project_mock)

        await service.create_issue_for_task(task_id="test-task-id")

        ensure_linear_project_mock.assert_awaited_once_with("team-123", "gobby")
        assert ensure_linear_project_mock.await_count == 1
        assert ensure_linear_project_mock.await_args is not None
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
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
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
    async def test_create_issue_reuses_existing_linear_issue_by_title(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        """create_issue_for_task links an exact existing Linear title before creating."""
        mock_task = MagicMock()
        mock_task.title = "Feature: Add new thing"
        mock_task.description = "Adds a cool feature"
        mock_task.linear_team_id = None
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42
        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.return_value = {
            "issues": [{"id": "lin-existing", "title": "#42: Feature: Add new thing"}]
        }

        result = await sync_service.create_issue_for_task(task_id="test-task-id")

        mock_mcp_manager.call_tool.assert_awaited_once()
        assert mock_mcp_manager.call_tool.call_args.kwargs["tool_name"] == "list_issues"
        mock_task_manager.update_task.assert_called_once_with(
            "test-task-id",
            linear_issue_id="lin-existing",
            linear_team_id="team-123",
        )
        assert result["linear_issue_id"] == "lin-existing"
        assert result["gobby_ref"] == "#42"

    @pytest.mark.asyncio
    async def test_create_issue_rejects_invalid_mcp_response(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        """create_issue_for_task validates create_issue responses before reading fields."""
        mock_task = MagicMock()
        mock_task.title = "Feature"
        mock_task.description = "Description"
        mock_task.linear_team_id = None
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42
        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.side_effect = [
            {"issues": []},
            ["not-a-dict"],
        ]

        with pytest.raises(LinearSyncError, match="expected dict, got list"):
            await sync_service.create_issue_for_task(task_id="test-task-id")

        assert mock_mcp_manager.call_tool.await_count == 2
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_issue_rejects_missing_mcp_issue_id(
        self,
        sync_service: LinearSyncService,
        mock_mcp_manager: MagicMock,
        mock_task_manager: MagicMock,
    ) -> None:
        """create_issue_for_task rejects create_issue responses without an id."""
        mock_task = MagicMock()
        mock_task.title = "Feature"
        mock_task.description = "Description"
        mock_task.linear_team_id = None
        mock_task.id = "test-task-id"
        mock_task.priority = 2
        mock_task.seq_num = 42
        mock_task_manager.get_task.return_value = mock_task
        mock_mcp_manager.call_tool.side_effect = [
            {"issues": []},
            {"title": "Feature"},
        ]

        with pytest.raises(LinearSyncError, match="missing required id"):
            await sync_service.create_issue_for_task(task_id="test-task-id")

        assert mock_mcp_manager.call_tool.await_count == 2
        mock_task_manager.update_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_issue_raises_when_no_team_id(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """create_missing_issues only selects unlinked non-closed tasks."""
        mock_task_manager.db.fetchall.return_value = []

        await sync_service.create_missing_issues()

        sql = mock_task_manager.db.fetchall.call_args.args[0]
        assert "linear_issue_id IS NULL" in sql
        assert "closed_at IS NULL" in sql

    @pytest.mark.asyncio
    async def test_create_missing_issues_applies_ordered_batch_limit(
        self,
        sync_service: LinearSyncService,
        mock_task_manager: MagicMock,
    ) -> None:
        to_thread = AsyncMock(return_value=[])

        with patch("gobby.sync.linear_task_ops.asyncio.to_thread", new=to_thread):
            await sync_service.create_missing_issues(limit=25)

        assert to_thread.await_args is not None
        fetchall, sql, params = to_thread.await_args.args
        assert fetchall is mock_task_manager.db.fetchall
        assert "ORDER BY seq_num NULLS LAST, created_at, id" in sql
        assert sql.endswith("LIMIT %s")
        assert params == (sync_service.project_id, 25)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, -1])
    async def test_create_missing_issues_rejects_non_positive_limit(
        self,
        sync_service: LinearSyncService,
        mock_task_manager: MagicMock,
        limit: int,
    ) -> None:
        create_issue = AsyncMock()

        with (
            patch.object(sync_service, "create_issue_for_task", new=create_issue),
            pytest.raises(ValueError, match="limit must be greater than zero"),
        ):
            await sync_service.create_missing_issues(limit=limit)

        mock_task_manager.db.fetchall.assert_not_called()
        create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_active_forward_creates_missing_and_pushes_active(
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """sync_active_forward creates missing active issues and skips pull."""
        create_missing_issues_mock = AsyncMock(return_value=[{"id": "lin-1"}])
        _replace_for_test(sync_service, "create_missing_issues", create_missing_issues_mock)
        push_active_tasks_mock = AsyncMock(
            return_value={"pushed": 2, "skipped": 0, "errors": 0, "deferred": 0}
        )
        _replace_for_test(sync_service, "push_active_tasks", push_active_tasks_mock)
        pull_linear_updates_mock = AsyncMock()
        _replace_for_test(sync_service, "pull_linear_updates", pull_linear_updates_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        result = await sync_service.sync_active_forward(team_id="team-123")

        assert result["mode"] == "forward_active"
        assert result["created_count"] == 1
        assert result["push"]["pushed"] == 2
        create_missing_issues_mock.assert_awaited_once_with(team_id="team-123")
        push_active_tasks_mock.assert_awaited_once_with()
        pull_linear_updates_mock.assert_not_called()
        update_synced_at_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_active_forward_keeps_cursor_when_push_has_errors(
        self, sync_service: LinearSyncService
    ) -> None:
        """sync_active_forward leaves synced_at unchanged when active push fails."""
        create_missing_issues_mock = AsyncMock(return_value=[])
        _replace_for_test(sync_service, "create_missing_issues", create_missing_issues_mock)
        push_active_tasks_mock = AsyncMock(
            return_value={"pushed": 1, "skipped": 0, "errors": 1, "deferred": 0}
        )
        _replace_for_test(sync_service, "push_active_tasks", push_active_tasks_mock)
        update_synced_at_mock = MagicMock()
        _replace_for_test(sync_service, "_update_synced_at", update_synced_at_mock)

        result = await sync_service.sync_active_forward(team_id="team-123")

        assert result["synced_at"] is None
        update_synced_at_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_active_tasks_filters_closed_tasks(
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """push_active_tasks only pushes linked non-closed tasks."""
        mock_task_manager.db.fetchall.return_value = [{"id": "task-1"}, {"id": "task-2"}]
        get_graphql_client_mock = AsyncMock(return_value=None)
        _replace_for_test(sync_service, "_get_graphql_client", get_graphql_client_mock)
        sync_task_to_linear_mock = AsyncMock()
        _replace_for_test(sync_service, "_sync_task_to_linear", sync_task_to_linear_mock)

        result = await sync_service.push_active_tasks()

        sql = mock_task_manager.db.fetchall.call_args.args[0]
        assert "linear_issue_id IS NOT NULL" in sql
        assert "closed_at IS NULL" in sql
        assert result == {"pushed": 2, "skipped": 0, "errors": 0, "deferred": 0}
        assert sync_task_to_linear_mock.await_count == 2

    async def test_push_active_tasks_reuses_graphql_client_and_team_states(
        self, sync_service: LinearSyncService, mock_task_manager: MagicMock
    ) -> None:
        """One push run shares GraphQL setup and state metadata across task updates."""
        loop_thread = threading.get_ident()
        storage_threads: list[int] = []

        tasks = {}
        for task_id, state in (("task-1", "ready"), ("task-2", "in_progress")):
            task = MagicMock()
            task.id = task_id
            task.seq_num = 1 if task_id == "task-1" else 2
            task.linear_issue_id = f"lin-{task_id}"
            task.linear_team_id = "team-123"
            task.title = f"Title {task_id}"
            task.description = f"Description {task_id}"
            task.priority = 2
            _set_task_state(task, state)
            tasks[task_id] = task

        def fetch_rows(*_args: object) -> list[dict[str, str]]:
            storage_threads.append(threading.get_ident())
            return [{"id": "task-1"}, {"id": "task-2"}]

        def get_task(task_id: str) -> MagicMock:
            storage_threads.append(threading.get_ident())
            return tasks[task_id]

        mock_task_manager.db.fetchall.side_effect = fetch_rows
        mock_task_manager.get_task.side_effect = get_task

        client = MagicMock()
        client.list_team_states = AsyncMock(
            return_value=[
                {"id": "state-todo", "name": "Todo"},
                {"id": "state-progress", "name": "In Progress"},
            ]
        )
        client.update_issue = AsyncMock(side_effect=[{"id": "lin-task-1"}, {"id": "lin-task-2"}])
        get_graphql_client_mock = AsyncMock(return_value=client)
        _replace_for_test(sync_service, "_get_graphql_client", get_graphql_client_mock)

        result = await sync_service.push_active_tasks()

        assert result == {"pushed": 2, "skipped": 0, "errors": 0, "deferred": 0}
        get_graphql_client_mock.assert_awaited_once_with()
        client.list_team_states.assert_awaited_once_with("team-123")
        assert [call.kwargs["state_id"] for call in client.update_issue.await_args_list] == [
            "state-todo",
            "state-progress",
        ]
        assert len(storage_threads) == 3
        assert all(thread_id != loop_thread for thread_id in storage_threads)


class TestLinearProjectBinding:
    """Test Linear project discovery and creation helpers."""

    @pytest.mark.asyncio
    async def test_ensure_linear_project_rechecks_by_name_after_mcp_create_failure(
        self, sync_service: LinearSyncService, mock_mcp_manager: MagicMock
    ) -> None:
        """ensure_linear_project handles races where another actor creates the project."""
        mock_mcp_manager.call_tool.side_effect = [
            {"projects": []},
            MCPError("already exists"),
            {"projects": [{"id": "lin-proj", "name": "gobby"}]},
        ]

        with patch(
            "gobby.sync.linear_project_ops.LinearGraphQLClient.from_database_async",
            new=AsyncMock(return_value=None),
        ) as graphql_factory:
            project, created = await sync_service.ensure_linear_project("team-123", "gobby")

        assert project == {"id": "lin-proj", "name": "gobby"}
        assert created is False
        assert mock_mcp_manager.call_tool.await_count == 3
        assert [
            call.kwargs["tool_name"] for call in mock_mcp_manager.call_tool.await_args_list
        ] == [
            "list_projects",
            "create_project",
            "list_projects",
        ]
        graphql_factory.assert_not_awaited()


class TestStateMapping:
    """Test state mapping functions."""

    def test_map_gobby_state_to_linear_ready(self, sync_service: LinearSyncService) -> None:
        """map_gobby_state_to_linear converts ready to Todo."""
        assert sync_service.map_gobby_state_to_linear("ready") == "Todo"

    def test_map_gobby_state_to_linear_in_progress(self, sync_service: LinearSyncService) -> None:
        """map_gobby_state_to_linear converts in_progress to In Progress."""
        assert sync_service.map_gobby_state_to_linear("in_progress") == "In Progress"

    def test_map_gobby_state_to_linear_closed(self, sync_service: LinearSyncService) -> None:
        """map_gobby_state_to_linear converts closed to Done."""
        assert sync_service.map_gobby_state_to_linear("closed") == "Done"

    def test_map_gobby_state_to_linear_unknown(self, sync_service: LinearSyncService) -> None:
        """map_gobby_state_to_linear defaults to Todo for unknown state."""
        assert sync_service.map_gobby_state_to_linear("unknown") == "Todo"

    def test_map_gobby_state_to_linear_escalated_defaults_to_todo(
        self, sync_service: LinearSyncService
    ) -> None:
        """Internal escalation is not a Linear cancellation state."""
        assert sync_service.map_gobby_state_to_linear("escalated") == "Todo"

    def test_map_linear_state_to_gobby_todo(self, sync_service: LinearSyncService) -> None:
        """map_linear_state_to_gobby converts Todo to ready."""
        assert sync_service.map_linear_state_to_gobby("Todo") == "ready"

    def test_map_linear_state_to_gobby_in_progress(self, sync_service: LinearSyncService) -> None:
        """map_linear_state_to_gobby converts In Progress to in_progress."""
        assert sync_service.map_linear_state_to_gobby("In Progress") == "in_progress"

    def test_map_linear_state_to_gobby_in_review(self, sync_service: LinearSyncService) -> None:
        """map_linear_state_to_gobby converts In Review to needs_review."""
        assert sync_service.map_linear_state_to_gobby("In Review") == "needs_review"

    def test_map_linear_state_to_gobby_done(self, sync_service: LinearSyncService) -> None:
        """map_linear_state_to_gobby converts Done to closed."""
        assert sync_service.map_linear_state_to_gobby("Done") == "closed"

    def test_map_linear_state_to_gobby_canceled(self, sync_service: LinearSyncService) -> None:
        """Explicit inbound Linear cancellation remains an escalation."""
        assert sync_service.map_linear_state_to_gobby("Canceled") == "escalated"

    def test_map_linear_state_to_gobby_unknown(self, sync_service: LinearSyncService) -> None:
        """map_linear_state_to_gobby defaults to ready for unknown state."""
        assert sync_service.map_linear_state_to_gobby("Unknown State") == "ready"


class TestLinearSyncIntegration:
    """Integration tests for full LinearSyncService workflows."""

    @pytest.mark.asyncio
    async def test_import_and_sync_workflow(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
            linear_project_id="lin-proj",
        )

        imported = await service.import_linear_issues()
        assert len(imported) == 1

        result = await service.sync_task_to_linear(task_id="gt-test123")
        assert result is not None
        update_call = mock_mcp_manager.call_tool.call_args_list[-1]
        assert "stateId" not in update_call.kwargs["arguments"]

    @pytest.mark.asyncio
    async def test_handles_empty_issue_list(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        """Test handling of team with no issues."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.return_value = {"issues": []}

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
            linear_project_id="lin-proj",
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


class TestLinearSyncErrorHandling:
    """Test error handling in sync operations."""

    @pytest.mark.asyncio
    async def test_sync_validates_response_structure(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
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
    async def test_error_recovery_network_failure(
        self, mock_mcp_manager: MagicMock, mock_task_manager: MagicMock
    ) -> None:
        """Test error handling when network fails."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"linear": MagicMock(state="connected")}
        mock_mcp_manager.call_tool.side_effect = Exception("Network error")

        service = LinearSyncService(
            mcp_manager=mock_mcp_manager,
            task_manager=mock_task_manager,
            project_id="test-project",
            linear_team_id="team-123",
            linear_project_id="lin-proj",
        )

        with pytest.raises(Exception, match="Network error"):
            await service.import_linear_issues()

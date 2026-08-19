"""Tests for project-scoped GitHub issue synchronization."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.github_triage import GitHubTriageConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.github_issue_sync import (
    GitHubIssueDeliveryHandler,
    GitHubIssueSyncService,
    GitHubRepositoryReadinessError,
)
from gobby.tasks.import_criteria import external_issue_validation_criteria

pytestmark = pytest.mark.unit

GitHubSyncFixture = tuple[
    GitHubIssueSyncService,
    MagicMock,
    MagicMock,
    MagicMock,
    MagicMock,
]


@pytest.fixture
def github_sync() -> GitHubSyncFixture:
    db = MagicMock()
    mcp_manager = MagicMock()
    task_manager = MagicMock()
    project_manager = MagicMock()
    project = MagicMock()
    project.id = "project-1"
    project.deleted_at = None
    project.github_repo = "owner/repo"
    project.github_url = None
    project.repo_path = None
    project_manager.get.return_value = project
    service = GitHubIssueSyncService(
        db=db,
        mcp_manager=mcp_manager,
        task_manager=task_manager,
        project_manager=project_manager,
    )
    service.config_store = MagicMock()
    service.config_store.get_config.return_value = GitHubTriageConfig(
        project_id="project-1",
        sync_enabled=True,
        repositories=("owner/repo",),
    )
    return service, db, mcp_manager, task_manager, project_manager


@pytest.mark.asyncio
async def test_webhook_issue_is_created_once_and_then_updated(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, task_manager, _ = github_sync
    validation_criteria = external_issue_validation_criteria("GitHub", "owner/repo#17")
    created_task = MagicMock(id="task-1")
    updated_task = MagicMock(id="task-1")
    task_manager.create_task.return_value = created_task
    task_manager.reconcile_task_state.return_value = updated_task
    db.fetchone.side_effect = [
        None,
        {
            "id": "task-1",
            "labels": ["local"],
            "validation_criteria": validation_criteria,
            "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ]
    issue = {
        "number": 17,
        "title": "Remote bug",
        "body": "Details",
        "labels": [{"name": "bug"}],
        "state": "open",
        "updated_at": datetime(2026, 1, 2, tzinfo=UTC).isoformat(),
    }

    first = await service.sync_issue("project-1", "owner/repo", 17, issue_data=issue)
    second = await service.sync_issue("project-1", "owner/repo", 17, issue_data=issue)

    assert first == {"action": "created", "task_id": "task-1"}
    assert second == {"action": "updated", "task_id": "task-1"}
    task_manager.create_task.assert_called_once_with(
        project_id="project-1",
        title="Remote bug",
        description="Details",
        labels=["bug"],
        github_issue_number=17,
        github_repo="owner/repo",
        validation_criteria=validation_criteria,
    )
    task_manager.update_task.assert_called_once_with(
        "task-1",
        labels=["local", "bug"],
    )
    assert "labels" not in task_manager.reconcile_task_state.call_args.kwargs


@pytest.mark.asyncio
async def test_pull_request_is_excluded_before_task_lookup(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, task_manager, _ = github_sync

    result = await service.sync_issue(
        "project-1",
        "owner/repo",
        18,
        issue_data={"number": 18, "pull_request": {"url": "https://example.test/pr/18"}},
    )

    assert result == {"action": "skipped_pull_request"}
    db.fetchone.assert_not_called()
    task_manager.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_closed_issue_closes_existing_linked_task(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, task_manager, _ = github_sync
    db.fetchone.return_value = {
        "id": "task-1",
        "labels": [],
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    task_manager.reconcile_task_state.return_value = MagicMock(id="task-1")
    closed_at = datetime(2026, 1, 2, tzinfo=UTC).isoformat()

    result = await service.sync_issue(
        "project-1",
        "owner/repo",
        19,
        issue_data={
            "number": 19,
            "title": "Done",
            "state": "closed",
            "closed_at": closed_at,
            "updated_at": datetime(2026, 1, 3, tzinfo=UTC).isoformat(),
        },
    )

    assert result["action"] == "updated"
    assert result["task_id"] == "task-1"
    updates = task_manager.reconcile_task_state.call_args.kwargs
    assert updates["closed_at"] == closed_at
    assert updates["closed_reason"] == "github_sync"


@pytest.mark.asyncio
async def test_local_newer_link_wins_over_recovery(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, task_manager, _ = github_sync
    remote_updated = datetime(2026, 1, 1, tzinfo=UTC)
    db.fetchone.return_value = {
        "id": "task-1",
        "labels": [],
        "updated_at": remote_updated + timedelta(minutes=1),
    }

    result = await service.sync_issue(
        "project-1",
        "owner/repo",
        20,
        issue_data={
            "number": 20,
            "title": "Stale remote title",
            "state": "open",
            "updated_at": remote_updated.isoformat(),
        },
    )

    assert result == {"action": "skipped_local_newer", "task_id": "task-1"}
    task_manager.reconcile_task_state.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_excludes_pull_requests(github_sync: GitHubSyncFixture) -> None:
    service, db, _, task_manager, _ = github_sync
    call_mock = AsyncMock(
        return_value={
            "issues": [
                {"number": 21, "title": "Issue", "state": "open"},
                {"number": 22, "title": "PR", "pull_request": {"url": "pr"}},
            ]
        }
    )
    db.fetchone.return_value = None
    task_manager.create_task.return_value = MagicMock(id="task-21")
    task_manager.reconcile_task_state.return_value = MagicMock(id="task-21")

    with patch.object(service, "_call", new=call_mock):
        stats = await service.recover_project("project-1")

    assert stats["scanned"] == 1
    assert stats["created"] == 1
    task_manager.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_recovery_normalizes_string_numbers_and_skips_malformed_numbers(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, task_manager, _ = github_sync
    call_mock = AsyncMock(
        return_value={
            "issues": [
                {"number": " 23 ", "title": "String number", "state": "open"},
                {"number": "not-a-number", "title": "Malformed", "state": "open"},
                {"number": 0, "title": "Non-positive", "state": "open"},
            ]
        }
    )
    db.fetchone.return_value = None
    task_manager.create_task.return_value = MagicMock(id="task-23")
    task_manager.reconcile_task_state.return_value = MagicMock(id="task-23")

    with patch.object(service, "_call", new=call_mock):
        stats = await service.recover_project("project-1")

    assert stats["scanned"] == 3
    assert stats["created"] == 1
    assert stats["errors"] == 2
    assert task_manager.create_task.call_args.kwargs["github_issue_number"] == 23


@pytest.mark.asyncio
async def test_recovery_stops_when_github_repeats_a_full_page(
    github_sync: GitHubSyncFixture,
) -> None:
    service, _, _, _, _ = github_sync
    page = [{"number": number} for number in range(1, 101)]
    call_mock = AsyncMock(side_effect=[{"issues": page}, {"issues": page}])
    sync_issue = AsyncMock(return_value={"action": "updated"})

    with (
        patch.object(service, "_call", new=call_mock),
        patch.object(service, "sync_issue", new=sync_issue),
    ):
        stats = await service.recover_project("project-1")

    assert call_mock.await_count == 2
    assert sync_issue.await_count == 100
    assert stats["scanned"] == 100
    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_recovery_stops_at_the_page_limit(
    github_sync: GitHubSyncFixture,
) -> None:
    service, _, _, _, _ = github_sync
    pages = [
        [{"number": number} for number in range(1, 101)],
        [{"number": number} for number in range(101, 201)],
    ]
    call_mock = AsyncMock(side_effect=[{"issues": page} for page in pages])
    sync_issue = AsyncMock(return_value={"action": "updated"})

    with (
        patch("gobby.sync.github_issue_sync._MAX_GITHUB_RECOVERY_PAGES", 2),
        patch.object(service, "_call", new=call_mock),
        patch.object(service, "sync_issue", new=sync_issue),
    ):
        stats = await service.recover_project("project-1")

    assert call_mock.await_count == 2
    assert sync_issue.await_count == 200
    assert stats["scanned"] == 200
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_sync_issue_offloads_synchronous_storage_and_manager_calls(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, task_manager, _ = github_sync
    db.fetchone.return_value = None
    task_manager.create_task.return_value = MagicMock(id="task-24")
    task_manager.reconcile_task_state.return_value = MagicMock(id="task-24")
    issue = {"number": 24, "title": "Offloaded", "state": "open"}
    offloaded: list[str] = []

    async def record_to_thread(
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        mock_name = getattr(func, "_mock_name", None)
        fallback_name = getattr(func, "__name__", type(func).__name__)
        offloaded.append(mock_name if isinstance(mock_name, str) else str(fallback_name))
        return func(*args, **kwargs)

    with patch("gobby.sync.github_issue_sync.asyncio.to_thread", side_effect=record_to_thread):
        result = await service.sync_issue(
            "project-1",
            "owner/repo",
            24,
            issue_data=issue,
        )

    assert result == {"action": "created", "task_id": "task-24"}
    assert {
        "get",
        "get_config",
        "repositories_for",
        "fetchone",
        "create_task",
        "reconcile_task_state",
    } <= set(offloaded)


@pytest.mark.asyncio
async def test_outbound_selects_only_fully_linked_tasks(
    github_sync: GitHubSyncFixture,
) -> None:
    service, db, _, _, _ = github_sync
    db.fetchall.return_value = [
        {"id": "task-1", "github_repo": "owner/repo", "github_issue_number": 7}
    ]
    remote_sync = MagicMock()
    remote_sync.sync_task_to_github = AsyncMock()

    with patch("gobby.sync.github_issue_sync.GitHubSyncService", return_value=remote_sync):
        stats = await service.push_linked_tasks("project-1", None)

    sql = db.fetchall.call_args.args[0]
    assert "github_repo IS NOT NULL" in sql
    assert "github_issue_number IS NOT NULL" in sql
    assert "updated_at >" not in sql
    remote_sync.sync_task_to_github.assert_awaited_once_with("task-1")
    assert stats == {"candidates": 1, "pushed": 1, "errors": 0}


@pytest.mark.asyncio
async def test_outbound_selects_tasks_updated_after_cursor(temp_db: HubDatabase) -> None:
    project_manager = LocalProjectManager(temp_db)
    project = project_manager.create(name="github-outbound-cursor", repo_path=None)
    task_manager = LocalTaskManager(temp_db)
    old_task = task_manager.create_task(
        project_id=project.id,
        title="Already pushed",
        github_repo="owner/repo",
        github_issue_number=7,
        validation_criteria="Linked issue updates are synchronized.",
    )
    dirty_task = task_manager.create_task(
        project_id=project.id,
        title="Dirty task",
        github_repo="owner/repo",
        github_issue_number=8,
        validation_criteria="Linked issue updates are synchronized.",
    )
    cursor = datetime(2026, 7, 30, 12, tzinfo=UTC)
    temp_db.execute(
        "UPDATE tasks SET updated_at = %s WHERE id = %s",
        (cursor - timedelta(seconds=1), old_task.id),
    )
    temp_db.execute(
        "UPDATE tasks SET updated_at = %s WHERE id = %s",
        (cursor + timedelta(seconds=1), dirty_task.id),
    )
    service = GitHubIssueSyncService(
        db=temp_db,
        mcp_manager=MagicMock(),
        task_manager=task_manager,
        project_manager=project_manager,
    )
    remote_sync = MagicMock()
    remote_sync.sync_task_to_github = AsyncMock()

    with patch("gobby.sync.github_issue_sync.GitHubSyncService", return_value=remote_sync):
        stats = await service.push_linked_tasks(project.id, cursor)

    assert remote_sync.sync_task_to_github.await_count == 1
    assert remote_sync.sync_task_to_github.await_args.args == (dirty_task.id,)
    assert stats == {"candidates": 1, "pushed": 1, "errors": 0}
    assert old_task.github_issue_number == 7


@pytest.mark.asyncio
async def test_outbound_logs_item_context_on_failure(
    github_sync: GitHubSyncFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, db, _, _, _ = github_sync
    db.fetchall.return_value = [
        {"id": "task-1", "github_repo": "owner/repo", "github_issue_number": 7}
    ]
    remote_sync = MagicMock()
    remote_sync.sync_task_to_github = AsyncMock(side_effect=PermissionError("credential rejected"))
    caplog.set_level("WARNING", logger="gobby.sync.github_issue_sync")

    with patch("gobby.sync.github_issue_sync.GitHubSyncService", return_value=remote_sync):
        stats = await service.push_linked_tasks("project-1", None)

    assert stats == {"candidates": 1, "pushed": 0, "errors": 1}
    assert "task-1" in caplog.text
    assert "owner/repo#7" in caplog.text
    assert "PermissionError" in caplog.text
    assert "credential rejected" in caplog.text


@pytest.mark.asyncio
async def test_outbound_propagates_rate_limit_retry_metadata(
    github_sync: GitHubSyncFixture,
) -> None:
    class RateLimited(RuntimeError):
        retry_after_seconds = 60

    service, db, _, _, _ = github_sync
    db.fetchall.return_value = [
        {"id": "task-1", "github_repo": "owner/repo", "github_issue_number": 7}
    ]
    remote_sync = MagicMock()
    remote_sync.sync_task_to_github = AsyncMock(side_effect=RateLimited("rate limit"))

    with (
        patch("gobby.sync.github_issue_sync.GitHubSyncService", return_value=remote_sync),
        pytest.raises(RateLimited) as raised,
    ):
        await service.push_linked_tasks("project-1", None)

    assert raised.value.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_sync_only_delivery_does_not_run_triage(github_sync: GitHubSyncFixture) -> None:
    service, _, _, _, _ = github_sync
    sync_issue = AsyncMock(return_value={"action": "updated", "task_id": "task-1"})
    triage = AsyncMock()
    triage_service = MagicMock()
    triage_service.store = service.config_store
    triage_service.triage_issue = triage
    handler = GitHubIssueDeliveryHandler(triage_service)
    handler.sync_service = service

    with patch.object(service, "sync_issue", new=sync_issue):
        result = await handler("project-1", "owner/repo", 23, source="webhook")

    assert result["action"] == "updated"
    assert result["task_id"] == "task-1"
    assert sync_issue.await_count == 1
    sync_issue.assert_awaited_once_with(
        "project-1",
        "owner/repo",
        23,
        issue_data=None,
    )
    assert triage.await_count == 0


@pytest.mark.asyncio
async def test_readiness_reports_repository_access_failure(
    github_sync: GitHubSyncFixture,
) -> None:
    service, _, mcp_manager, _, project_manager = github_sync
    mcp_manager.call_tool = AsyncMock(side_effect=RuntimeError("404 Not Found"))
    config = cast(MagicMock, service.config_store).get_config.return_value

    with pytest.raises(GitHubRepositoryReadinessError, match="owner/repo.*404 Not Found"):
        await service.check_access(
            project_manager.get.return_value,
            config,
        )


@pytest.mark.asyncio
async def test_readiness_preserves_rate_limit_retry_time(
    github_sync: GitHubSyncFixture,
) -> None:
    class RateLimited(RuntimeError):
        retry_after_seconds = 75

    service, _, mcp_manager, _, project_manager = github_sync
    mcp_manager.call_tool = AsyncMock(side_effect=RateLimited("rate limit"))
    config = cast(MagicMock, service.config_store).get_config.return_value

    with pytest.raises(GitHubRepositoryReadinessError) as raised:
        await service.check_access(
            project_manager.get.return_value,
            config,
        )

    assert vars(raised.value)["retry_after_seconds"] == 75

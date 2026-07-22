"""Tests for project-scoped GitHub issue synchronization."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.github_triage import GitHubTriageConfig
from gobby.sync.github_issue_sync import (
    GitHubIssueDeliveryHandler,
    GitHubIssueSyncService,
    GitHubRepositoryReadinessError,
)


@pytest.fixture
def github_sync() -> tuple[GitHubIssueSyncService, MagicMock, MagicMock, MagicMock, MagicMock]:
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
async def test_webhook_issue_is_created_once_and_then_updated(github_sync) -> None:
    service, db, _, task_manager, _ = github_sync
    created_task = MagicMock(id="task-1")
    updated_task = MagicMock(id="task-1")
    task_manager.create_task.return_value = created_task
    task_manager.reconcile_task_state.return_value = updated_task
    db.fetchone.side_effect = [
        None,
        {
            "id": "task-1",
            "labels": ["local"],
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
    )
    assert task_manager.reconcile_task_state.call_args.kwargs["labels"] == ["local", "bug"]


@pytest.mark.asyncio
async def test_pull_request_is_excluded_before_task_lookup(github_sync) -> None:
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
async def test_closed_issue_closes_existing_linked_task(github_sync) -> None:
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
    updates = task_manager.reconcile_task_state.call_args.kwargs
    assert updates["closed_at"] == closed_at
    assert updates["closed_reason"] == "github_sync"


@pytest.mark.asyncio
async def test_local_newer_link_wins_over_recovery(github_sync) -> None:
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
async def test_recovery_excludes_pull_requests(github_sync) -> None:
    service, db, _, task_manager, _ = github_sync
    service._call = AsyncMock(
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

    stats = await service.recover_project("project-1")

    assert stats["scanned"] == 1
    assert stats["created"] == 1
    task_manager.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_outbound_selects_only_fully_linked_tasks(github_sync) -> None:
    service, db, _, _, _ = github_sync
    db.fetchall.return_value = [
        {"id": "task-1", "github_repo": "owner/repo", "github_issue_number": 7}
    ]
    remote_sync = MagicMock()
    remote_sync.sync_task_to_github = AsyncMock()

    with patch("gobby.sync.github_issue_sync.GitHubSyncService", return_value=remote_sync):
        stats = await service.push_linked_tasks("project-1")

    sql = db.fetchall.call_args.args[0]
    assert "github_repo IS NOT NULL" in sql
    assert "github_issue_number IS NOT NULL" in sql
    remote_sync.sync_task_to_github.assert_awaited_once_with("task-1")
    assert stats == {"pushed": 1, "errors": 0}


@pytest.mark.asyncio
async def test_outbound_propagates_rate_limit_retry_metadata(github_sync) -> None:
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
        await service.push_linked_tasks("project-1")

    assert raised.value.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_sync_only_delivery_does_not_run_triage(github_sync) -> None:
    service, _, _, _, _ = github_sync
    service.sync_issue = AsyncMock(return_value={"action": "updated", "task_id": "task-1"})
    triage = AsyncMock()
    triage_service = MagicMock()
    triage_service.store = service.config_store
    triage_service.triage_issue = triage
    handler = GitHubIssueDeliveryHandler(triage_service)
    handler.sync_service = service

    result = await handler("project-1", "owner/repo", 23, source="webhook")

    assert result["action"] == "updated"
    triage.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_reports_repository_access_failure(github_sync) -> None:
    service, _, mcp_manager, _, project_manager = github_sync
    mcp_manager.call_tool = AsyncMock(side_effect=RuntimeError("404 Not Found"))

    with pytest.raises(GitHubRepositoryReadinessError, match="owner/repo.*404 Not Found"):
        await service.check_access(
            project_manager.get.return_value,
            service.config_store.get_config.return_value,
        )


@pytest.mark.asyncio
async def test_readiness_preserves_rate_limit_retry_time(github_sync) -> None:
    class RateLimited(RuntimeError):
        retry_after_seconds = 75

    service, _, mcp_manager, _, project_manager = github_sync
    mcp_manager.call_tool = AsyncMock(side_effect=RateLimited("rate limit"))

    with pytest.raises(GitHubRepositoryReadinessError) as raised:
        await service.check_access(
            project_manager.get.return_value,
            service.config_store.get_config.return_value,
        )

    assert raised.value.retry_after_seconds == 75

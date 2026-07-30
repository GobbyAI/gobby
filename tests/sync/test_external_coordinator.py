"""Focused tests for daemon-managed external issue synchronization."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.storage.external_issue_sync import ExternalIssueSyncStatus
from gobby.storage.github_triage import GitHubTriageConfig
from gobby.sync.external_coordinator import ExternalIssueSyncCoordinator

pytestmark = pytest.mark.unit


def _project(project_id: str = "project-1", *, linear_enabled: bool = True) -> MagicMock:
    project = MagicMock()
    project.id = project_id
    project.name = project_id
    project.deleted_at = None
    project.linear_sync_enabled = linear_enabled
    project.linear_team_id = "team-1" if linear_enabled else None
    project.linear_project_id = "linear-project-1" if linear_enabled else None
    project.github_repo = None
    return project


def _coordinator(
    projects: list[MagicMock],
    *,
    monotonic: Callable[[], float] = lambda: 0.0,
) -> tuple[ExternalIssueSyncCoordinator, MagicMock]:
    db = MagicMock()
    task_manager = MagicMock()
    task_manager.db = db
    project_manager = MagicMock()
    project_manager.list.return_value = projects
    coordinator = ExternalIssueSyncCoordinator(
        db=db,
        mcp_manager=MagicMock(),
        task_manager=task_manager,
        project_manager=project_manager,
        monotonic=monotonic,
    )
    coordinator.github_config_store = MagicMock()
    coordinator.github_config_store.get_config.side_effect = lambda project_id, *_: (
        GitHubTriageConfig(project_id=project_id)
    )
    coordinator.status_store = MagicMock()
    coordinator.status_store.get.return_value = None
    coordinator.status_store.counts.return_value = (0, 0)
    return coordinator, project_manager


def _status_store(coordinator: ExternalIssueSyncCoordinator) -> MagicMock:
    return cast(MagicMock, coordinator.status_store)


def _github_status(cursor: datetime | None) -> ExternalIssueSyncStatus:
    return ExternalIssueSyncStatus(
        project_id="project-1",
        provider="github",
        state="healthy",
        last_outbound_success_at=cursor,
    )


def _github_service() -> MagicMock:
    service = MagicMock()
    service.check_access = AsyncMock(return_value=("owner/repo",))
    service.push_linked_tasks = AsyncMock(return_value={"candidates": 1, "pushed": 1, "errors": 0})
    service.recover_project = AsyncMock(
        return_value={"created": 0, "updated": 0, "skipped": 0, "errors": 0}
    )
    return service


@pytest.mark.asyncio
async def test_enablement_is_discovered_without_daemon_restart() -> None:
    project = _project(linear_enabled=False)
    coordinator, _ = _coordinator([project])
    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(return_value=[])
    linear.sync_all = AsyncMock(return_value={"pull": {}, "push": {}})

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        assert linear.create_missing_issues.await_count == 0

        project.linear_sync_enabled = True
        project.linear_team_id = "team-1"
        project.linear_project_id = "linear-project-1"
        await coordinator.refresh()
        await coordinator.wait_for_idle()

    linear.create_missing_issues.assert_awaited_once_with("team-1", limit=25)
    status_store = _status_store(coordinator)
    states = [call.kwargs["state"] for call in status_store.upsert.call_args_list]
    assert "disabled" in states
    assert "healthy" in states
    healthy = next(
        call for call in status_store.upsert.call_args_list if call.kwargs["state"] == "healthy"
    )
    assert healthy.kwargs["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_disable_during_run_keeps_disabled_status() -> None:
    project = _project()
    coordinator, _ = _coordinator([project])
    entered = asyncio.Event()
    release = asyncio.Event()
    linear = MagicMock()
    linear.is_available.return_value = True

    async def create_missing(*_args: object, **_kwargs: object) -> list[object]:
        entered.set()
        await release.wait()
        return []

    linear.create_missing_issues = AsyncMock(side_effect=create_missing)
    linear.sync_all = AsyncMock(return_value={"pull": {}, "push": {}})

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        await entered.wait()

        project.linear_sync_enabled = False
        await coordinator.refresh()
        release.set()
        await coordinator.wait_for_idle()

    states = [call.kwargs["state"] for call in _status_store(coordinator).upsert.call_args_list]
    assert states[-1] == "disabled"
    assert "running" in states
    assert linear.create_missing_issues.await_count == 1
    assert linear.sync_all.await_count == 0


@pytest.mark.asyncio
async def test_linear_backfill_runs_ordered_batches_every_five_seconds() -> None:
    now = [0.0]
    coordinator, _ = _coordinator([_project()], monotonic=lambda: now[0])
    linear_counts = iter(
        [
            (0, 30),
            (25, 5),
            (25, 5),
            (30, 0),
        ]
    )
    _status_store(coordinator).counts.side_effect = (
        lambda _project_id, provider: next(linear_counts) if provider == "linear" else (0, 0)
    )
    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(
        side_effect=[
            [{"id": str(index)} for index in range(25)],
            [{"id": str(index)} for index in range(5)],
        ]
    )
    linear.sync_all = AsyncMock(return_value={"pull": {}, "push": {}})

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        await coordinator.wait_for_idle()
        assert coordinator._due[("linear", "project-1")] == 5.0

        now[0] = 4.9
        await coordinator.refresh()
        assert linear.create_missing_issues.await_count == 1

        now[0] = 5.0
        await coordinator.refresh()
        await coordinator.wait_for_idle()

    assert linear.create_missing_issues.await_count == 2
    assert all(call.kwargs["limit"] == 25 for call in linear.create_missing_issues.await_args_list)
    assert coordinator._due[("linear", "project-1")] == 305.0


@pytest.mark.asyncio
async def test_one_project_failure_does_not_block_another() -> None:
    projects = [_project("failing"), _project("healthy")]
    coordinator, _ = _coordinator(projects)

    def make_service(*, project_id: str, **_: object) -> MagicMock:
        service = MagicMock()
        service.is_available.return_value = True
        if project_id == "failing":
            service.create_missing_issues = AsyncMock(side_effect=RuntimeError("provider failed"))
        else:
            service.create_missing_issues = AsyncMock(return_value=[])
        service.sync_all = AsyncMock(return_value={"pull": {}, "push": {}})
        return service

    with patch("gobby.sync.external_coordinator.LinearSyncService", side_effect=make_service):
        await coordinator.refresh()
        await coordinator.wait_for_idle()

    final_states = {
        (call.kwargs["project_id"], call.kwargs["state"])
        for call in _status_store(coordinator).upsert.call_args_list
    }
    assert ("failing", "degraded") in final_states
    assert ("healthy", "healthy") in final_states


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["rate-limit exceeded", "HTTP response 429"])
async def test_rate_limit_retry_time_is_respected(message: str) -> None:
    class RateLimited(RuntimeError):
        retry_after_seconds = 42

    now = [10.0]
    project = _project()
    coordinator, _ = _coordinator([project], monotonic=lambda: now[0])
    current = MagicMock(consecutive_failures=1, last_error="limited")
    _status_store(coordinator).get.return_value = current
    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(side_effect=RateLimited(message))

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        await coordinator.wait_for_idle()

    assert coordinator._due[("linear", "project-1")] == 52.0
    calls = _status_store(coordinator).upsert.call_args_list
    assert any(call.kwargs["state"] == "rate_limited" for call in calls)
    assert any(call.kwargs["retry_at"] is not None for call in calls)


@pytest.mark.asyncio
async def test_usage_limit_uses_maximum_backoff() -> None:
    now = [10.0]
    project = _project()
    coordinator, _ = _coordinator([project], monotonic=lambda: now[0])
    current = MagicMock(consecutive_failures=1, last_error="usage limit exceeded")
    _status_store(coordinator).get.return_value = current
    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(side_effect=RuntimeError("usage limit exceeded"))

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        await coordinator.wait_for_idle()

    assert coordinator._due[("linear", "project-1")] == 310.0
    assert any(
        call.kwargs["state"] == "rate_limited"
        for call in _status_store(coordinator).upsert.call_args_list
    )


@pytest.mark.asyncio
async def test_issue_429_error_does_not_rate_limit_project() -> None:
    now = [10.0]
    project = _project()
    coordinator, _ = _coordinator([project], monotonic=lambda: now[0])
    current = MagicMock(consecutive_failures=1, last_error="missing")
    _status_store(coordinator).get.return_value = current
    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(side_effect=RuntimeError("Issue #429 not found"))

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        await coordinator.wait_for_idle()

    assert coordinator._due[("linear", "project-1")] == 15.0
    calls = _status_store(coordinator).upsert.call_args_list
    assert any(call.kwargs["state"] == "degraded" for call in calls)
    assert all(call.kwargs["state"] != "rate_limited" for call in calls)


@pytest.mark.asyncio
async def test_wait_for_idle_drains_dispatched_work() -> None:
    coordinator, _ = _coordinator([_project()])
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def create_missing(*_args: object, **_kwargs: object) -> list[object]:
        entered.set()
        await release.wait()
        finished.set()
        return []

    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(side_effect=create_missing)
    linear.sync_all = AsyncMock(return_value={"pull": {}, "push": {}})

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator.refresh()
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        draining = asyncio.create_task(coordinator.wait_for_idle())
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not draining.done()

        release.set()
        await draining
    assert finished.is_set()


@pytest.mark.asyncio
async def test_run_survives_recoverable_refresh_failure() -> None:
    coordinator, _ = _coordinator([])
    coordinator.refresh_interval_seconds = 0.01
    shutdown = asyncio.Event()
    attempts = 0

    async def refresh() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database failure")
        shutdown.set()

    with patch.object(coordinator, "refresh", new=AsyncMock(side_effect=refresh)):
        await asyncio.wait_for(coordinator.run(shutdown), timeout=1.0)

    assert attempts == 2


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_dispatched_work() -> None:
    coordinator, _ = _coordinator([_project()])
    shutdown = asyncio.Event()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def create_missing(*_args: object, **_kwargs: object) -> list[object]:
        entered.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("blocking operation unexpectedly completed")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(side_effect=create_missing)
    linear.sync_all = AsyncMock(return_value={"pull": {}, "push": {}})

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        run_task = asyncio.create_task(coordinator.run(shutdown))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        shutdown.set()
        await asyncio.wait_for(run_task, timeout=1.0)

    assert cancelled.is_set()
    assert not coordinator._tasks
    assert any(
        call.kwargs["state"] == "pending"
        for call in _status_store(coordinator).upsert.call_args_list
    )


@pytest.mark.asyncio
async def test_github_outbound_advances_cursor_to_run_boundary() -> None:
    project = _project(linear_enabled=False)
    project.github_repo = "owner/repo"
    coordinator, _ = _coordinator([project])
    previous = datetime(2026, 7, 30, 10, tzinfo=UTC)
    attempt = datetime(2026, 7, 30, 11, tzinfo=UTC)
    completed = datetime(2026, 7, 30, 12, tzinfo=UTC)
    _status_store(coordinator).get.return_value = _github_status(previous)
    service = _github_service()
    config = GitHubTriageConfig(project_id=project.id, sync_enabled=True)

    with (
        patch("gobby.sync.external_coordinator.GitHubIssueSyncService", return_value=service),
        patch("gobby.sync.external_coordinator.utc_now", side_effect=[attempt, completed]),
    ):
        await coordinator._run_github(project, config, outbound=True, recovery=False)

    service.push_linked_tasks.assert_awaited_once_with(project.id, previous)
    final = _status_store(coordinator).upsert.call_args_list[-1].kwargs
    assert final["state"] == "healthy"
    assert final["last_outbound_success_at"] == attempt
    assert final["last_success_at"] == completed


@pytest.mark.asyncio
async def test_github_outbound_failure_preserves_cursor() -> None:
    project = _project(linear_enabled=False)
    coordinator, _ = _coordinator([project])
    previous = datetime(2026, 7, 30, 10, tzinfo=UTC)
    _status_store(coordinator).get.return_value = _github_status(previous)
    service = _github_service()
    service.push_linked_tasks.return_value = {"candidates": 1, "pushed": 0, "errors": 1}
    config = GitHubTriageConfig(project_id=project.id, sync_enabled=True)

    with patch(
        "gobby.sync.external_coordinator.GitHubIssueSyncService",
        return_value=service,
    ):
        await coordinator._run_github(project, config, outbound=True, recovery=False)

    final = _status_store(coordinator).upsert.call_args_list[-1].kwargs
    assert final["state"] == "degraded"
    assert final["last_outbound_success_at"] == previous


@pytest.mark.asyncio
async def test_github_triage_recovery_runs_deliveries_before_repositories() -> None:
    project = _project(linear_enabled=False)
    coordinator, _ = _coordinator([project])
    previous = datetime(2026, 7, 30, 10, tzinfo=UTC)
    _status_store(coordinator).get.return_value = _github_status(previous)
    service = _github_service()
    triage = MagicMock()
    triage.recover_deliveries = AsyncMock(return_value={"recovered": 1, "retried": 0, "errors": 0})
    triage.reconcile_project_repos = AsyncMock(
        return_value={"scanned": 1, "triaged": 1, "errors": 0}
    )
    operations = MagicMock()
    operations.attach_mock(triage.recover_deliveries, "recover_deliveries")
    operations.attach_mock(triage.reconcile_project_repos, "reconcile_project_repos")
    config = GitHubTriageConfig(project_id=project.id, triage_enabled=True)

    with (
        patch("gobby.sync.external_coordinator.GitHubIssueSyncService", return_value=service),
        patch("gobby.sync.external_coordinator.GitHubIssueTriageService", return_value=triage),
    ):
        await coordinator._run_github(project, config, outbound=False, recovery=True)

    assert operations.mock_calls == [
        call.recover_deliveries(project.id),
        call.reconcile_project_repos(project.id),
    ]
    final = _status_store(coordinator).upsert.call_args_list[-1].kwargs
    assert final["state"] == "healthy"
    assert final["last_outbound_success_at"] == previous
    assert final["last_statistics"]["triage"] == {
        "deliveries": {"recovered": 1, "retried": 0, "errors": 0},
        "repositories": {"scanned": 1, "triaged": 1, "errors": 0},
    }


@pytest.mark.asyncio
async def test_github_delivery_recovery_errors_degrade_after_successful_outbound() -> None:
    project = _project(linear_enabled=False)
    coordinator, _ = _coordinator([project])
    previous = datetime(2026, 7, 30, 10, tzinfo=UTC)
    attempt = datetime(2026, 7, 30, 11, tzinfo=UTC)
    _status_store(coordinator).get.return_value = _github_status(previous)
    service = _github_service()
    triage = MagicMock()
    triage.recover_deliveries = AsyncMock(return_value={"recovered": 0, "retried": 0, "errors": 1})
    triage.reconcile_project_repos = AsyncMock(
        return_value={"scanned": 0, "triaged": 0, "errors": 0}
    )
    config = GitHubTriageConfig(
        project_id=project.id,
        sync_enabled=True,
        triage_enabled=True,
    )

    with (
        patch("gobby.sync.external_coordinator.GitHubIssueSyncService", return_value=service),
        patch("gobby.sync.external_coordinator.GitHubIssueTriageService", return_value=triage),
        patch("gobby.sync.external_coordinator.utc_now", return_value=attempt),
    ):
        await coordinator._run_github(project, config, outbound=True, recovery=True)

    final = _status_store(coordinator).upsert.call_args_list[-1].kwargs
    assert final["state"] == "degraded"
    assert final["last_outbound_success_at"] == attempt
    assert final["last_statistics"]["triage"]["deliveries"]["errors"] == 1


@pytest.mark.asyncio
async def test_github_recovery_skips_triage_when_disabled() -> None:
    project = _project(linear_enabled=False)
    coordinator, _ = _coordinator([project])
    service = _github_service()
    config = GitHubTriageConfig(project_id=project.id, sync_enabled=True)

    with (
        patch("gobby.sync.external_coordinator.GitHubIssueSyncService", return_value=service),
        patch("gobby.sync.external_coordinator.GitHubIssueTriageService") as triage_factory,
    ):
        await coordinator._run_github(project, config, outbound=False, recovery=True)

    service.recover_project.assert_awaited_once_with(project.id)
    triage_factory.assert_not_called()

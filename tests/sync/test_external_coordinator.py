"""Focused tests for daemon-managed external issue synchronization."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.storage.github_triage import GitHubTriageConfig
from gobby.sync.external_coordinator import ExternalIssueSyncCoordinator


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
    monotonic=lambda: 0.0,
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
    states = [call.kwargs["state"] for call in coordinator.status_store.upsert.call_args_list]
    assert "disabled" in states
    assert "healthy" in states


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

    states = [call.kwargs["state"] for call in coordinator.status_store.upsert.call_args_list]
    assert states[-1] == "disabled"
    linear.sync_all.assert_not_awaited()


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
    coordinator.status_store.counts.side_effect = (
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
        for call in coordinator.status_store.upsert.call_args_list
    }
    assert ("failing", "degraded") in final_states
    assert ("healthy", "healthy") in final_states


@pytest.mark.asyncio
async def test_rate_limit_retry_time_is_respected() -> None:
    class RateLimited(RuntimeError):
        retry_after_seconds = 42

    now = [10.0]
    coordinator, _ = _coordinator([_project()], monotonic=lambda: now[0])
    current = MagicMock(consecutive_failures=1, last_error="limited")
    coordinator.status_store.get.side_effect = [None, None, None, current, current]
    linear = MagicMock()
    linear.is_available.return_value = True
    linear.create_missing_issues = AsyncMock(side_effect=RateLimited("rate limit exceeded"))

    with patch("gobby.sync.external_coordinator.LinearSyncService", return_value=linear):
        await coordinator._run_linear(_project())

    assert coordinator._due[("linear", "project-1")] == 52.0
    calls = coordinator.status_store.upsert.call_args_list
    assert any(call.kwargs["state"] == "rate_limited" for call in calls)
    assert any(call.kwargs["retry_at"] is not None for call in calls)


@pytest.mark.asyncio
async def test_wait_for_idle_drains_dispatched_work() -> None:
    coordinator, _ = _coordinator([])
    release = asyncio.Event()
    finished = asyncio.Event()

    async def operation() -> None:
        await release.wait()
        finished.set()

    coordinator._start("linear", "project-1", operation())
    draining = asyncio.create_task(coordinator.wait_for_idle())
    await asyncio.sleep(0)
    assert not draining.done()

    release.set()
    await draining
    assert finished.is_set()

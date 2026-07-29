"""Tests for the daemon-owned memory dream coordinator."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream.coordinator import MemoryDreamCoordinator
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.service import MemoryDreamService

pytestmark = pytest.mark.unit


def _service_mock() -> MagicMock:
    service = MagicMock()
    service.start_async = AsyncMock()
    service.start_all_due_projects_async = AsyncMock()
    service.execute_run = AsyncMock()
    service.execute_all_due_projects_run = AsyncMock()
    service.record_run_failure = MagicMock()
    return service


def _coordinator(service: MagicMock) -> MemoryDreamCoordinator:
    return MemoryDreamCoordinator(cast(MemoryDreamService, service))


@pytest.mark.asyncio
async def test_trigger_admits_and_launches_executor() -> None:
    service = _service_mock()
    service.start_async.return_value = {"success": True, "run_id": "run-1"}
    service.execute_run.return_value = {"success": True}
    coordinator = _coordinator(service)
    options = DreamRunOptions(project_id="proj-1")

    result = await coordinator.trigger(options)

    assert result == {
        "success": True,
        "run_id": "run-1",
        "status": "running",
        "coalesced": False,
    }
    tasks = coordinator.background_tasks()
    assert len(tasks) == 1
    assert tasks[0].get_name() == "memory-dream:run-1"
    await asyncio.gather(*tasks)
    assert coordinator.background_tasks() == ()
    service.execute_run.assert_awaited_once_with("run-1", options)


@pytest.mark.asyncio
async def test_trigger_all_due_threads_options_to_executor() -> None:
    service = _service_mock()
    service.start_all_due_projects_async.return_value = {"success": True, "run_id": "agg-1"}
    service.execute_all_due_projects_run.return_value = {"success": True}
    coordinator = _coordinator(service)

    result = await coordinator.trigger_all_due_projects(dry_run=True, full_sweep=True)

    assert result["run_id"] == "agg-1"
    await asyncio.gather(*coordinator.background_tasks())
    service.start_all_due_projects_async.assert_awaited_once_with(
        dry_run=True,
        skip_consolidation=False,
        memory_type=None,
        full_sweep=True,
    )
    service.execute_all_due_projects_run.assert_awaited_once_with(
        "agg-1",
        dry_run=True,
        skip_consolidation=False,
        memory_type=None,
        full_sweep=True,
    )


@pytest.mark.asyncio
async def test_coalesced_admission_does_not_launch_second_executor() -> None:
    service = _service_mock()
    active = {"run_id": "run-1", "phase": "sweep", "checkpoint": {"batch_number": 2}}
    service.start_async.return_value = {
        "success": True,
        "run_id": "run-1",
        "coalesced": True,
        "active": active,
    }
    coordinator = _coordinator(service)

    result = await coordinator.trigger(DreamRunOptions(project_id="proj-1"))

    assert result["status"] == "running"
    assert result["coalesced"] is True
    assert result["active"] == active
    assert coordinator.background_tasks() == ()
    service.execute_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_admission_returns_error_code() -> None:
    service = _service_mock()
    service.start_async.return_value = {
        "success": False,
        "error": "a memory dream run is already active with incompatible options",
        "conflict": {"run_id": "other-1", "scope": "all", "phase": "sweep"},
    }
    coordinator = _coordinator(service)

    result = await coordinator.trigger(DreamRunOptions(project_id="proj-1"))

    assert result["success"] is False
    assert result["error_code"] == "dream_run_conflict"
    assert result["conflict"]["run_id"] == "other-1"
    assert coordinator.background_tasks() == ()


@pytest.mark.asyncio
async def test_disabled_failure_passes_through_without_error_code() -> None:
    service = _service_mock()
    service.start_async.return_value = {"success": False, "error": "memory dream is disabled"}
    coordinator = _coordinator(service)

    result = await coordinator.trigger(DreamRunOptions(project_id="proj-1"))

    assert result == {"success": False, "error": "memory dream is disabled"}


@pytest.mark.asyncio
async def test_launch_failure_records_terminal_failed_row() -> None:
    service = _service_mock()
    service.start_async.return_value = {"success": True, "run_id": "run-1"}
    service.execute_run.return_value = {"success": True}
    coordinator = _coordinator(service)

    with patch(
        "gobby.memory.dream.coordinator.asyncio.create_task",
        side_effect=RuntimeError("loop closed"),
    ):
        result = await coordinator.trigger(DreamRunOptions(project_id="proj-1"))

    assert result["success"] is False
    assert result["run_id"] == "run-1"
    assert result["status"] == "failed"
    assert "loop closed" in result["error"]
    service.record_run_failure.assert_called_once()
    assert coordinator.background_tasks() == ()


@pytest.mark.asyncio
async def test_executor_crash_records_failure_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service_mock()
    service.start_async.return_value = {"success": True, "run_id": "run-1"}
    service.execute_run.side_effect = RuntimeError("boom")
    coordinator = _coordinator(service)
    caplog.set_level(logging.WARNING, logger="gobby.memory.dream.coordinator")

    await coordinator.trigger(DreamRunOptions(project_id="proj-1"))
    await asyncio.gather(*coordinator.background_tasks(), return_exceptions=True)

    service.record_run_failure.assert_called_once()
    assert "run-1" in service.record_run_failure.call_args.args[0]
    assert any(
        "Background memory dream task failed" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_failed_result_logs_warning_without_failure_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _service_mock()
    service.start_async.return_value = {"success": True, "run_id": "run-1"}
    service.execute_run.return_value = {"success": False, "error": "no progress"}
    coordinator = _coordinator(service)
    caplog.set_level(logging.WARNING, logger="gobby.memory.dream.coordinator")

    await coordinator.trigger(DreamRunOptions(project_id="proj-1"))
    await asyncio.gather(*coordinator.background_tasks())

    # The executor already persisted the failed status on its run row.
    service.record_run_failure.assert_not_called()
    messages = [record.getMessage() for record in caplog.records]
    assert any("Background memory dream failed" in message for message in messages)
    assert any("no progress" in message for message in messages)


@pytest.mark.asyncio
async def test_aclose_cancels_live_background_task() -> None:
    service = _service_mock()
    service.start_async.return_value = {"success": True, "run_id": "run-1"}
    release = asyncio.Event()

    async def _blocked_execute(run_id: str, options: Any) -> dict[str, Any]:
        await release.wait()
        return {"success": True}

    service.execute_run = AsyncMock(side_effect=_blocked_execute)
    coordinator = _coordinator(service)

    await coordinator.trigger(DreamRunOptions(project_id="proj-1"))
    assert len(coordinator.background_tasks()) == 1

    await coordinator.aclose()

    assert coordinator.background_tasks() == ()
    # The run row was not force-failed: cancellation is an interruption, not a
    # failure, and restart recovery owns the row state.
    service.record_run_failure.assert_not_called()

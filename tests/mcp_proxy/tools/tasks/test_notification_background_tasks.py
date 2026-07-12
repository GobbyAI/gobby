"""Background-task lifecycle coverage for task notifications and signoff relays."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._notifications import (
    _notification_tasks,
    notify_parent_on_task_state_change,
)
from gobby.mcp_proxy.tools.tasks._stage_review import (
    _relay_signoff_to_build_coordinator,
    _schedule_signoff_relay,
    _signoff_relay_tasks,
)


@pytest.fixture(autouse=True)
async def clear_notification_tasks():
    yield
    tasks = [*_notification_tasks.values(), *_signoff_relay_tasks.values()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _notification_tasks.clear()
    _signoff_relay_tasks.clear()


@pytest.mark.asyncio
async def test_parent_notification_is_retained_until_completion() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def pending_notification(*_args) -> None:
        started.set()
        await release.wait()

    with patch(
        "gobby.mcp_proxy.tools.tasks._notifications._notify",
        new=pending_notification,
    ):
        registry = InternalToolRegistry("notification-test")
        registry.register(
            name="schedule_notification",
            description="Schedule a notification from a synchronous internal tool",
            input_schema={"type": "object", "properties": {}},
            func=lambda: notify_parent_on_task_state_change(
                MagicMock(), "task-1", "in_progress", "#1"
            ),
        )
        await registry.call("schedule_notification", {})
        await asyncio.wait_for(started.wait(), timeout=1)

        assert len(_notification_tasks) == 1
        task = next(iter(_notification_tasks.values()))
        assert task.done() is False
        assert task.get_name() == "gobby-parent-notification-task-1-in_progress"
        cleanup_finished = asyncio.Event()
        task.add_done_callback(lambda _task: cleanup_finished.set())

        release.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=1)

    assert _notification_tasks == {}


@pytest.mark.asyncio
async def test_signoff_relay_is_retained_until_completion() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def pending_relay(*_args, **_kwargs) -> None:
        started.set()
        await release.wait()

    task = SimpleNamespace(project_id="project-1")
    with patch(
        "gobby.mcp_proxy.tools.tasks._stage_review._relay_signoff_to_build_coordinator",
        new=pending_relay,
    ):
        _schedule_signoff_relay(
            MagicMock(),
            task=task,
            task_id="task-1",
            stage_name="review",
            action="approve_review",
            from_session_id="session-1",
            signoff_message="approved",
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        assert len(_signoff_relay_tasks) == 1
        background_task = next(iter(_signoff_relay_tasks.values()))
        assert background_task.done() is False
        assert background_task.get_name() == "gobby-review-signoff-relay-approve_review"
        cleanup_finished = asyncio.Event()
        background_task.add_done_callback(lambda _task: cleanup_finished.set())

        release.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=1)

    assert _signoff_relay_tasks == {}


@pytest.mark.asyncio
async def test_signoff_relay_surfaces_non_database_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = SimpleNamespace(project_id="project-1")
    with (
        patch(
            "gobby.mcp_proxy.tools.tasks._stage_review._relay_signoff_to_build_coordinator_sync",
            side_effect=RuntimeError("delivery transport unavailable"),
        ),
        caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.tools.tasks._stage_review"),
    ):
        await _relay_signoff_to_build_coordinator(
            MagicMock(),
            task=task,
            task_id="task-1",
            stage_name="review",
            action="approve_review",
            from_session_id="session-1",
            signoff_message="approved",
        )

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage() == "Failed to relay review signoff to build coordinator"
    assert record.task_id == "task-1"
    assert record.stage_name == "review"
    assert record.action == "approve_review"
    assert record.exc_info is not None

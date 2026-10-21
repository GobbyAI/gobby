"""Lifecycle coverage for task notifications and synchronous signoff relays."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._notifications import (
    _notification_tasks,
    _notify,
    notify_parent_on_task_state_change,
)


@pytest.fixture(autouse=True)
async def clear_notification_tasks():
    yield
    tasks = list(_notification_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _notification_tasks.clear()


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
async def test_parent_notification_broadcast_includes_escalation_event_id() -> None:
    class RecordingWebsocket:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        async def broadcast_task_event(self, **event: object) -> None:
            self.events.append(event)

    db = MagicMock()
    db.fetchone.return_value = {"id": "run-1", "parent_session_id": "parent-1"}
    websocket = RecordingWebsocket()
    app_context = SimpleNamespace(websocket_server=websocket)

    with patch("gobby.app_context.get_app_context", return_value=app_context):
        await _notify(db, "task-1", "escalated", "#1", "event-1")

    assert websocket.events == [
        {
            "event": "task_progress",
            "task_id": "task-1",
            "state": "escalated",
            "ref": "#1",
            "parent_session_id": "parent-1",
            "run_id": "run-1",
            "event_id": "event-1",
        }
    ]

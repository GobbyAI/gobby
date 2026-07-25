"""Tests for routing cron completion events through communications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.models import CommsMessage
from gobby.runner_broadcasting import setup_cron_event_broadcasting
from gobby.storage.cron_models import CronJob, CronRun

pytestmark = pytest.mark.unit
NOW = datetime(2026, 2, 10, tzinfo=UTC)


class RecordingCronCommunications:
    """Record scheduled-run events without a live communications channel."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_event(
        self,
        event_type: str,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
        *,
        event_id: str | None = None,
    ) -> list[CommsMessage]:
        self.events.append(
            {
                "event_type": event_type,
                "content": content,
                "project_id": project_id,
                "session_id": session_id,
                "event_id": event_id,
            }
        )
        return []


def _job() -> CronJob:
    return CronJob(
        id="cj-1",
        project_id="project-1",
        name="Nightly backup",
        schedule_type="cron",
        action_type="handler",
        action_config={},
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_completion_routes_to_comms_without_websocket() -> None:
    scheduler = MagicMock()
    communications = RecordingCronCommunications()
    setup_cron_event_broadcasting(None, scheduler, communications)
    run = CronRun(
        id="cr-1",
        cron_job_id="cj-1",
        triggered_at=NOW,
        created_at=NOW,
        status="completed",
        output="backup ok",
    )

    await scheduler.on_run_complete(_job(), run)

    assert communications.events == [
        {
            "event_type": "cron.run.completed",
            "content": 'Scheduled job "Nightly backup" completed.\n\nbackup ok',
            "project_id": "project-1",
            "session_id": None,
            "event_id": "cr-1",
        }
    ]


@pytest.mark.asyncio
async def test_websocket_failure_does_not_block_comms_delivery() -> None:
    websocket_server = AsyncMock()
    websocket_server.broadcast_cron_event.side_effect = RuntimeError("websocket unavailable")
    scheduler = MagicMock()
    communications = RecordingCronCommunications()
    setup_cron_event_broadcasting(websocket_server, scheduler, communications)
    run = CronRun(
        id="cr-1",
        cron_job_id="cj-1",
        triggered_at=NOW,
        created_at=NOW,
        status="failed",
        error="backup failed",
    )

    await scheduler.on_run_complete(_job(), run)

    assert communications.events == [
        {
            "event_type": "cron.run.failed",
            "content": 'Scheduled job "Nightly backup" failed.\n\nError: backup failed',
            "project_id": "project-1",
            "session_id": None,
            "event_id": "cr-1",
        }
    ]

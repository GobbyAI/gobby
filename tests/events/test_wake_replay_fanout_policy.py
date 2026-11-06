"""Replay never live-wakes a parked seat with no claimed task for fanout rows alone.

A fanout row sent while a seat was still active keeps its wake intent. When that seat
later parks between turns with no claimed task, turn-end and startup replay must leave
the row queued for the seat's next natural turn instead of waking it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append((session_id, priority))
        return {"session_id": session_id, "delivered": True, "method": "fake"}


def _session(session_manager: SessionManager, project_id: str, external_id: str) -> str:
    return session_manager.register(
        external_id=external_id,
        machine_id=None,
        source="codex",
        project_id=project_id,
    ).id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claimed", "direct_row", "expect_wake"),
    [(False, False, False), (True, False, True), (False, True, True)],
    ids=["fanout-only-parked-unclaimed-stays-queued", "claimed-seat-wakes", "direct-row-wakes"],
)
async def test_replay_leaves_fanout_rows_queued_for_a_parked_seat_without_a_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    claimed: bool,
    direct_row: bool,
    expect_wake: bool,
) -> None:
    project_id = sample_project["id"]
    sender_id = _session(session_manager, project_id, "sender")
    seat_id = _session(session_manager, project_id, "parked-seat")
    session_manager.update_status(seat_id, "idle")
    if claimed:
        LocalTaskManager(temp_db).create_task(
            project_id=project_id,
            title="Claimed while parked",
            claimed_by_session_id=seat_id,
            category="code",
            validation_criteria="Claim keeps the replayed wake.",
            implementation_domain="backend",
        )
    messages = InterSessionMessageManager(temp_db)
    # Sent while the seat was active, so the row still carries its wake intent.
    messages.create_message(
        from_session=sender_id,
        to_session=seat_id,
        content="daemon notice",
        metadata_json=json.dumps({"wake_requested": True, "broadcast_id": "broadcast-1"}),
    )
    if direct_row:
        messages.create_message(
            from_session=sender_id,
            to_session=seat_id,
            content="direct work",
            metadata_json='{"wake_requested": true}',
        )
    dispatcher = _RecordingDispatcher()
    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=dispatcher,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())

    await coordinator.open()

    assert dispatcher.calls == ([(seat_id, "normal")] if expect_wake else [])
    assert messages.get_undelivered_wake_messages(seat_id)

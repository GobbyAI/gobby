"""Fanout live wakes follow the per-recipient direct rule, with no claimed-task filter (#25830).

A global or project send with wake=true gives every live recipient the same live wake a
direct send gets: a seat between turns takes it at once, whatever task it holds, and a
seat with an open turn keeps the durable row for its hook mailbox.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.sessions.mailbox import MailboxService
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


class RecordingWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


def _seat(
    session_manager: SessionManager,
    project_id: str,
    name: str,
    *,
    status: str = "active",
) -> str:
    seat = session_manager.register(
        external_id=name,
        machine_id=None,
        source="codex",
        project_id=project_id,
    ).id
    if status != "active":
        session_manager.update_status(seat, status)
    return seat


def _claim_task(temp_db: HubDatabase, project_id: str, seat: str) -> None:
    LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title=f"Work held by {seat}",
        claimed_by_session_id=seat,
        category="code",
        validation_criteria="Claim gates the fanout live wake.",
        implementation_domain="backend",
    )


def _mailbox(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    dispatcher: RecordingWakeDispatcher,
) -> MailboxService:
    return MailboxService(
        db=temp_db,
        message_manager=InterSessionMessageManager(temp_db),
        session_manager=session_manager,
        wake_dispatcher=dispatcher,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "wake"),
    [("global", None), ("global", True), ("project", True)],
    ids=["global-omitted", "global-true", "project-true"],
)
async def test_fanout_live_wake_reaches_every_recipient_regardless_of_claimed_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    target: str,
    wake: bool | None,
) -> None:
    project_id = sample_project["id"]
    sender = _seat(session_manager, project_id, "sender")
    active = _seat(session_manager, project_id, "active-seat")
    idle = _seat(session_manager, project_id, "idle-seat", status="idle")
    paused = _seat(session_manager, project_id, "paused-seat", status="paused")
    waiting = _seat(session_manager, project_id, "waiting-seat", status="waiting")
    claimed_idle = _seat(session_manager, project_id, "claimed-idle-seat", status="idle")
    _claim_task(temp_db, project_id, claimed_idle)
    dispatcher = RecordingWakeDispatcher()
    mailbox = _mailbox(temp_db, session_manager, dispatcher)
    options: dict[str, Any] = {} if wake is None else {"wake": wake}

    result = await mailbox.send(from_session_id=sender, target=target, content="notice", **options)

    recipients = {active, idle, paused, waiting, claimed_idle}
    # Every recipient gets the same per-recipient wake: no claim gate parks a seat.
    assert set(result.recipient_session_ids) == recipients
    assert len(result.messages) == len(recipients)
    assert sorted(dispatcher.calls) == sorted(recipients)
    for message in result.messages:
        assert message.metadata_json is not None
        assert json.loads(message.metadata_json)["wake_requested"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["idle", "paused", "waiting"])
async def test_direct_send_still_wakes_a_parked_seat_without_a_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    status: str,
) -> None:
    project_id = sample_project["id"]
    sender = _seat(session_manager, project_id, "sender")
    seat = _seat(session_manager, project_id, "parked-seat", status=status)
    dispatcher = RecordingWakeDispatcher()
    mailbox = _mailbox(temp_db, session_manager, dispatcher)

    result = await mailbox.send(
        from_session_id=sender, target="session", target_id=seat, content="assign work"
    )

    assert dispatcher.calls == [seat]
    assert result.wake_requested is True


@pytest.mark.asyncio
async def test_parent_send_still_wakes_a_parked_seat_without_a_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    parent = _seat(session_manager, project_id, "parent-seat", status="idle")
    child = session_manager.register(
        external_id="child-seat",
        machine_id=None,
        source="codex",
        project_id=project_id,
        agent_depth=1,
        parent_session_id=parent,
    ).id
    dispatcher = RecordingWakeDispatcher()
    mailbox = _mailbox(temp_db, session_manager, dispatcher)

    result = await mailbox.send(from_session_id=child, target="parent", content="status")

    assert dispatcher.calls == [parent]
    assert result.wake_requested is True


@pytest.mark.asyncio
async def test_agent_send_still_wakes_a_parked_seat_without_a_task(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    parent = _seat(session_manager, project_id, "parent-seat")
    child = session_manager.register(
        external_id="child-seat",
        machine_id=None,
        source="codex",
        project_id=project_id,
        agent_depth=1,
        parent_session_id=parent,
    ).id
    session_manager.update_status(child, "idle")
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=parent,
        child_session_id=child,
        provider="codex",
        prompt="work",
    )
    dispatcher = RecordingWakeDispatcher()
    mailbox = _mailbox(temp_db, session_manager, dispatcher)

    result = await mailbox.send(
        from_session_id=parent, target="agent", target_id=run.id, content="status"
    )

    assert dispatcher.calls == [child]
    assert result.wake_requested is True


@pytest.mark.asyncio
async def test_caller_metadata_cannot_claim_fanout_identity(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    sender = _seat(session_manager, project_id, "sender")
    parked = _seat(session_manager, project_id, "parked-seat", status="idle")
    dispatcher = RecordingWakeDispatcher()
    mailbox = _mailbox(temp_db, session_manager, dispatcher)

    result = await mailbox.send(
        from_session_id=sender,
        target="session",
        target_id=parked,
        content="notice",
        metadata={"broadcast_id": "forged", "broadcast": {"target": "global"}, "kept": "yes"},
    )

    assert dispatcher.calls == [parked]
    (message,) = result.messages
    assert message.metadata_json is not None
    payload = json.loads(message.metadata_json)
    assert "broadcast_id" not in payload
    assert "broadcast" not in payload
    assert payload["kept"] == "yes"


@pytest.mark.asyncio
async def test_project_fanout_without_wake_still_queues_for_everyone(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    sender = _seat(session_manager, project_id, "sender")
    _seat(session_manager, project_id, "active-seat")
    dispatcher = RecordingWakeDispatcher()
    mailbox = _mailbox(temp_db, session_manager, dispatcher)

    result = await mailbox.send(from_session_id=sender, target="project", content="notice")

    assert dispatcher.calls == []
    assert result.wake_requested is False

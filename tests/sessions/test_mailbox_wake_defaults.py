"""An omitted wake takes the target default: global wakes, project fanout queues."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.sessions.mailbox import MailboxService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class RecordingWakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]:
        self.calls.append(session_id)
        return {"session_id": session_id, "delivered": True, "method": "fake"}


def _sessions(session_manager: SessionManager, project_id: str) -> tuple[str, str]:
    sender, recipient = (
        session_manager.register(
            external_id=name,
            machine_id=None,
            source="codex",
            project_id=project_id,
        ).id
        for name in ("sender", "recipient")
    )
    return sender, recipient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "wake", "expect_wake"),
    [
        ("global", None, True),
        ("project", None, False),
        ("global", False, False),
    ],
    ids=["global-omitted-wakes", "project-omitted-queues", "global-false-queues"],
)
async def test_fanout_wake_default(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    target: str,
    wake: bool | None,
    expect_wake: bool,
) -> None:
    sender, recipient = _sessions(session_manager, sample_project["id"])
    dispatcher = RecordingWakeDispatcher()
    mailbox = MailboxService(
        db=temp_db,
        message_manager=InterSessionMessageManager(temp_db),
        session_manager=session_manager,
        wake_dispatcher=dispatcher,
    )
    options: dict[str, Any] = {} if wake is None else {"wake": wake}

    result = await mailbox.send(from_session_id=sender, target=target, content="hi", **options)

    assert result.recipient_session_ids == [recipient]
    assert dispatcher.calls == ([recipient] if expect_wake else [])
    metadata_json = result.messages[0].metadata_json
    assert metadata_json is not None
    assert json.loads(metadata_json)["wake_requested"] is expect_wake

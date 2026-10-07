"""Event-time replay ordering through real HTTP ingress, adapter and turn storage."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI

from gobby.hooks import envelope_dedupe, inbox
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse
from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY
from gobby.hooks.session_types import HookSessionManager
from gobby.sessions.turn_lifecycle import TurnLifecycleReducer
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.integration


class LifecycleHooks:
    """Use production turn/Stop handlers while excluding unrelated rule execution."""

    def __init__(self, sessions: SessionManager) -> None:
        self.session_manager = sessions
        self._session_manager = sessions
        self.handlers = EventHandlers(session_manager=cast(HookSessionManager, sessions))
        self.posted: list[HookEventType] = []

    def handle(self, event: HookEvent) -> HookResponse:
        self.posted.append(event.event_type)
        if event.event_type == HookEventType.BEFORE_AGENT:
            self.handlers._begin_turn_lifecycle(event)
            return HookResponse(decision="allow")
        return self.handlers.handle_stop(event)


def envelope(
    session_id: str, hook_type: str, timestamp: datetime, source: str = "claude"
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": timestamp.isoformat(),
        "source": source,
        "hook_type": hook_type,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "input_data": {
            "session_id": "replay-order",
            "prompt": "work",
            "timestamp": (timestamp + timedelta(days=1)).isoformat(),
        },
        "headers": {"X-Gobby-Session-Id": session_id},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "grok"])
@pytest.mark.parametrize(
    "types",
    [
        ["UserPromptSubmit", "Stop"],
        ["Stop", "UserPromptSubmit", "Stop"],
    ],
)
async def test_backlog_keeps_later_same_session_events(
    route_hook_replay_to_app: Callable[[FastAPI], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    types: list[str],
    source: str,
) -> None:
    session = session_manager.register(
        external_id="replay-order",
        source=source,
        machine_id=None,
        project_id=sample_project["id"],
    )
    session_manager.update_status(session.id, "paused")
    manager = LifecycleHooks(session_manager)
    server = create_http_server(session_manager=session_manager)
    server.app.state.hook_manager = manager
    route_hook_replay_to_app(server.app)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "isolated-test-token")
    monkeypatch.setattr(
        envelope_dedupe, "get_processed_envelope_dir", lambda _=None: tmp_path / "processed"
    )
    pending = tmp_path / "inbox"
    pending.mkdir()
    start = utc_now() - timedelta(minutes=10)
    times = [start + timedelta(minutes=index) for index in range(len(types))]
    for index, (hook_type, timestamp) in enumerate(zip(types, times, strict=True)):
        (pending / f"{index:04}-order.json").write_text(
            json.dumps(envelope(session.id, hook_type, timestamp, source))
        )
    assert await inbox.drain_hook_inbox_once(server.app, pending, include_fresh=True) == len(types)
    assert manager.posted == [
        HookEventType.BEFORE_AGENT if value == "UserPromptSubmit" else HookEventType.STOP
        for value in types
    ]
    after = session_manager.get(session.id)
    assert after is not None and after.status == "paused"
    expected_start = times[types.index("UserPromptSubmit")]
    assert TurnLifecycleReducer(session_manager).get(session.id).started_at == expected_start
    assert not inbox.get_hook_quarantine_dir(pending).exists()


@pytest.mark.asyncio
async def test_missed_stop_after_live_same_status_prompt_is_archived(
    route_hook_replay_to_app: Callable[[FastAPI], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = session_manager.register(
        external_id="replay-order",
        source="claude",
        machine_id=None,
        project_id=sample_project["id"],
    )
    manager = LifecycleHooks(session_manager)
    server = create_http_server(session_manager=session_manager)
    server.app.state.hook_manager = manager
    route_hook_replay_to_app(server.app)
    monkeypatch.setattr(inbox, "read_local_api_token", lambda: "isolated-test-token")
    monkeypatch.setattr(
        envelope_dedupe, "get_processed_envelope_dir", lambda _=None: tmp_path / "processed"
    )
    now = utc_now()
    assert (
        await inbox._post_envelope(
            envelope(session.id, "UserPromptSubmit", now - timedelta(minutes=10))
        )
    ).status_code == 200
    first = session_manager.get(session.id)
    assert first is not None and first.status == "active"
    pending = tmp_path / "inbox"
    pending.mkdir()
    old_stop = pending / "old-stop.json"
    old_stop.write_text(json.dumps(envelope(session.id, "Stop", now - timedelta(minutes=5))))
    assert (
        await inbox._post_envelope(envelope(session.id, "UserPromptSubmit", now))
    ).status_code == 200
    lifecycle = TurnLifecycleReducer(session_manager).get(session.id)
    assert lifecycle.generation == 2
    assert lifecycle.started_at == now
    second = session_manager.get(session.id)
    assert second is not None and second.last_activity == first.last_activity
    assert await inbox.drain_hook_inbox_once(server.app, pending, include_fresh=True) == 1
    assert manager.posted == [HookEventType.BEFORE_AGENT, HookEventType.BEFORE_AGENT]
    after = session_manager.get(session.id)
    assert after is not None and after.status == "active"
    archive = inbox.get_hook_quarantine_dir(pending) / f"{old_stop.name}.meta.json"
    assert json.loads(archive.read_text())["reason"] == "superseded_hook"
    # Reordered live starts must never move the durable event clock backwards.
    assert (
        await inbox._post_envelope(
            envelope(session.id, "UserPromptSubmit", now - timedelta(minutes=1))
        )
    ).status_code == 200
    assert TurnLifecycleReducer(session_manager).get(session.id).started_at == now

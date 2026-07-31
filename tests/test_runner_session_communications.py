"""Isolated integration tests for session status communications."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsRoutingRule
from gobby.communications.session_events import route_session_status_transition
from gobby.config.communications import ChannelDefaults, CommunicationsConfig
from gobby.runner_broadcasting import setup_session_status_communications
from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _channel(name: str) -> ChannelConfig:
    now = datetime.now(UTC)
    return ChannelConfig(
        id=str(uuid.uuid4()),
        channel_type="telegram",
        name=name,
        enabled=True,
        config_json={"default_destination": f"chat-{name}"},
        created_at=now,
        updated_at=now,
    )


def _rule(
    name: str,
    channel: ChannelConfig,
    *,
    project_id: str | None,
    enabled: bool = True,
) -> CommsRoutingRule:
    now = datetime.now(UTC)
    return CommsRoutingRule(
        id=str(uuid.uuid4()),
        name=name,
        channel_id=channel.id,
        event_pattern="session.agent.paused",
        project_id=project_id,
        priority=0,
        enabled=enabled,
        config_json={},
        created_at=now,
        updated_at=now,
    )


def _telegram_adapter(platform_message_id: str) -> MagicMock:
    adapter = MagicMock()
    adapter.channel_type = "telegram"
    adapter.supports_webhooks = True
    adapter.supports_polling = False
    adapter.initialize = AsyncMock()
    adapter.send_message = AsyncMock(
        side_effect=[f"{platform_message_id}-{index}" for index in range(1, 10)]
    )
    adapter.shutdown = AsyncMock()
    return adapter


def _transition(session_id: str, project_id: str) -> SessionStatusTransition:
    return SessionStatusTransition(
        session_id=session_id,
        project_id=project_id,
        agent_run_id=str(uuid.uuid4()),
        status="paused",
        transitioned_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        seq_num=42,
        title="Existing agent",
        source="codex",
    )


async def test_session_event_routing_is_scoped_deduplicated_and_administrable(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    project_b = LocalProjectManager(temp_db).create("session-comms-project-b")
    session_manager = SessionManager(temp_db)
    session_a = session_manager.register(
        external_id="session-comms-project-a",
        machine_id="machine-a",
        source="codex",
        project_id=sample_project["id"],
    )
    session_b = session_manager.register(
        external_id="session-comms-project-b",
        machine_id="machine-b",
        source="codex",
        project_id=project_b.id,
    )
    channels = {
        name: _channel(name) for name in ("project-a", "project-b", "global", "disabled", "deleted")
    }
    store = LocalCommunicationsStore(temp_db)
    for channel in channels.values():
        store.create_channel(channel)

    rules = {
        "project-a": _rule(
            "project-a",
            channels["project-a"],
            project_id=sample_project["id"],
        ),
        "project-b": _rule(
            "project-b",
            channels["project-b"],
            project_id=project_b.id,
        ),
        "global": _rule("global", channels["global"], project_id=None),
        "disabled": _rule(
            "disabled",
            channels["disabled"],
            project_id=sample_project["id"],
            enabled=False,
        ),
        "deleted": _rule(
            "deleted",
            channels["deleted"],
            project_id=sample_project["id"],
        ),
    }
    for rule in rules.values():
        store.create_routing_rule(rule)
    store.delete_routing_rule(rules["deleted"].id)

    adapters = {name: _telegram_adapter(f"platform-{name}") for name in channels}
    adapter_factory = MagicMock(
        side_effect=[
            *[adapters[channel.name] for channel in channels.values()],
            _telegram_adapter("platform-gobby-chat"),
        ]
    )
    manager = CommunicationsManager(
        CommunicationsConfig(
            enabled=True,
            channel_defaults=ChannelDefaults(rate_limit_per_minute=60, burst=10),
        ),
        store,
        MagicMock(),
        MagicMock(),
    )
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=adapter_factory,
    ):
        await manager.start()

    transition_a = _transition(session_a.id, sample_project["id"])
    transition_b = _transition(session_b.id, project_b.id)
    first = await route_session_status_transition(manager, transition_a)
    replay = await route_session_status_transition(manager, transition_a)
    second = await route_session_status_transition(manager, transition_b)

    assert len(first) == len(replay) == len(second) == 2
    assert {message.channel_id for message in first} == {
        channels["project-a"].id,
        channels["global"].id,
    }
    assert {message.channel_id for message in second} == {
        channels["project-b"].id,
        channels["global"].id,
    }
    assert {message.id for message in replay} == {message.id for message in first}
    adapters["project-a"].send_message.assert_awaited_once()
    adapters["project-b"].send_message.assert_awaited_once()
    assert adapters["global"].send_message.await_count == 2
    adapters["disabled"].send_message.assert_not_awaited()
    adapters["deleted"].send_message.assert_not_awaited()

    persisted = store.list_messages(limit=100)
    assert len(persisted) == 4
    source_event_ids = {message.metadata_json["source_event_id"] for message in persisted}
    assert len(source_event_ids) == 2
    assert all(message.status == "sent" for message in persisted)


async def test_session_bridge_registers_without_websocket(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_manager = SessionManager(temp_db)
    delivered = asyncio.Event()
    communications_manager = MagicMock()

    async def send_event(*_args: object, **_kwargs: object) -> list[object]:
        delivered.set()
        return []

    communications_manager.handle_session_status_transition = AsyncMock(side_effect=send_event)
    daemon_loop = asyncio.get_running_loop()
    listener = setup_session_status_communications(
        session_manager,
        communications_manager,
        lambda: daemon_loop,
    )
    transition = _transition(str(uuid.uuid4()), sample_project["id"])

    await asyncio.to_thread(listener, transition)
    await asyncio.wait_for(delivered.wait(), timeout=1.0)

    assert listener in session_manager._status_transition_listeners
    communications_manager.handle_session_status_transition.assert_awaited_once_with(transition)

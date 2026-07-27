from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.communications.responder import (
    CommunicationsResponder,
    ResponderContext,
)
from gobby.runner_broadcasting import setup_communications_event_broadcasting


def make_channel(
    *,
    config: dict[str, Any] | None = None,
    channel_id: str = "channel-1",
    name: str = "telegram",
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        channel_type="telegram",
        name=name,
        enabled=True,
        config_json=config
        or {
            "responder": {"enabled": True},
            "allow_from": ["owner"],
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def make_message(
    *,
    content: str = "hello",
    sender_id: str = "owner",
    conversation_id: str = "chat-1",
    session_id: str = "session-1",
    group: bool = False,
    mentioned: bool = True,
) -> CommsMessage:
    return CommsMessage(
        id=f"message-{conversation_id}-{content}",
        channel_id="channel-1",
        direction="inbound",
        content=content,
        identity_id="internal-identity-id",
        session_id=session_id,
        metadata_json={
            "external_user_id": sender_id,
            "platform_channel_id": conversation_id,
            "conversation_type": "group" if group else "private",
            "mentioned": mentioned,
        },
        created_at=datetime.now(UTC),
    )


class FakeManager:
    def __init__(self, channel: ChannelConfig) -> None:
        self.channel = channel
        self.sent: list[dict[str, Any]] = []
        self.reactions: list[tuple[str, str, str, str | None]] = []

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        if channel_id == self.channel.id:
            return self.channel
        return None

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommsMessage:
        self.sent.append(
            {
                "channel_name": channel_name,
                "content": content,
                "session_id": session_id,
                "metadata": metadata,
            }
        )
        return CommsMessage(
            id=f"outbound-{len(self.sent)}",
            channel_id=self.channel.id,
            direction="outbound",
            content=content,
            session_id=session_id,
            metadata_json=metadata or {},
            created_at=datetime.now(UTC),
        )

    async def set_reaction(
        self,
        channel_name: str,
        conversation_id: str,
        platform_message_id: str,
        reaction: str | None,
    ) -> None:
        self.reactions.append((channel_name, conversation_id, platform_message_id, reaction))


class RecordingBackend:
    def __init__(self, *, turn_response: str | None = None) -> None:
        self.turn_response = turn_response
        self.turns: list[ResponderContext] = []
        self.commands: list[tuple[str, ResponderContext]] = []

    async def run_turn(self, context: ResponderContext) -> str | None:
        self.turns.append(context)
        return self.turn_response

    async def new_session(self, context: ResponderContext) -> str | None:
        self.commands.append(("new", context))
        return "new"

    async def reset_session(self, context: ResponderContext) -> str | None:
        self.commands.append(("reset", context))
        return "reset"

    async def stop_turn(self, context: ResponderContext) -> str | None:
        self.commands.append(("stop", context))
        return "stop"

    async def status(self, context: ResponderContext) -> str | None:
        self.commands.append(("status", context))
        return "status"

    async def help(self, context: ResponderContext) -> str | None:
        self.commands.append(("help", context))
        return "help"


class RecordingWebSocket:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def broadcast_communications_event(
        self,
        *,
        event: str,
        **kwargs: Any,
    ) -> None:
        self.events.append((event, kwargs))


class RecordingEventResponder:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.fail = fail

    async def handle_event(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))
        if self.fail:
            raise RuntimeError("responder failed")


@pytest.mark.asyncio
async def test_access_gate_accepts_allowlisted_sender() -> None:
    manager = FakeManager(make_channel())
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)

    task = await responder.handle_message(make_message())
    assert task is not None
    await task

    assert [context.sender_id for context in backend.turns] == ["owner"]


@pytest.mark.asyncio
async def test_blank_attachment_content_does_not_start_responder_turn() -> None:
    manager = FakeManager(make_channel())
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)

    task = await responder.handle_message(make_message(content=""))

    assert task is None
    assert backend.turns == []


@pytest.mark.asyncio
async def test_access_gate_rejects_sender_outside_allowlist(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = FakeManager(make_channel())
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)
    caplog.set_level(logging.INFO, logger="gobby.communications.responder")

    task = await responder.handle_message(make_message(sender_id="stranger"))

    assert task is None
    assert backend.turns == []
    assert "stranger" in caplog.text


async def test_expected_mention_required_skip_logs_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = FakeManager(
        make_channel(
            config={
                "responder": {"enabled": True},
                "group_policy": "open",
                "require_mention": True,
            }
        )
    )
    responder = CommunicationsResponder(manager, backend=RecordingBackend())
    message = make_message(group=True, mentioned=False)
    logger_name = "gobby.communications.responder"
    expected_message = "Ignoring group message for conversation chat-1: mention_required"

    with caplog.at_level(logging.DEBUG, logger=logger_name):
        task = await responder.handle_message(message)

    assert task is None
    records = [
        record
        for record in caplog.records
        if record.getMessage() == expected_message
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger_name):
        await responder.handle_message(message)

    assert not any(
        record.getMessage() == expected_message
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("group_policy", "groups", "sender_id", "mentioned", "accepted"),
    [
        pytest.param("disabled", {"chat-1": {}}, "owner", True, False, id="disabled"),
        pytest.param("allowlist", {}, "owner", True, False, id="group-not-allowlisted"),
        pytest.param(
            "allowlist",
            {"chat-1": {}},
            "stranger",
            True,
            False,
            id="sender-not-allowlisted",
        ),
        pytest.param(
            "allowlist",
            {"chat-1": {}},
            "owner",
            False,
            False,
            id="mention-required",
        ),
        pytest.param(
            "allowlist",
            {"chat-1": {}},
            "owner",
            True,
            True,
            id="allowlisted-and-mentioned",
        ),
        pytest.param(
            "allowlist",
            {"chat-1": {"require_mention": False}},
            "owner",
            False,
            True,
            id="mention-disabled-for-group",
        ),
        pytest.param("open", {}, "stranger", True, True, id="open"),
    ],
)
@pytest.mark.asyncio
async def test_group_policy_and_mention_gate(
    group_policy: str,
    groups: dict[str, dict[str, Any]],
    sender_id: str,
    mentioned: bool,
    accepted: bool,
) -> None:
    manager = FakeManager(
        make_channel(
            config={
                "responder": {"enabled": True},
                "allow_from": ["owner"],
                "group_policy": group_policy,
                "require_mention": True,
                "groups": groups,
            }
        )
    )
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)

    task = await responder.handle_message(
        make_message(sender_id=sender_id, group=True, mentioned=mentioned)
    )
    if task is not None:
        await task

    assert bool(backend.turns) is accepted


@pytest.mark.parametrize(
    ("command", "expected_handler"),
    [
        ("new", "new"),
        ("reset", "reset"),
        ("stop", "stop"),
        ("status", "status"),
        ("help", "help"),
    ],
)
@pytest.mark.asyncio
async def test_each_command_routes_to_its_backend_handler(
    command: str,
    expected_handler: str,
) -> None:
    manager = FakeManager(make_channel())
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)

    task = await responder.handle_message(make_message(content=f"/{command}"))

    assert task is None
    assert [name for name, _context in backend.commands] == [expected_handler]
    assert manager.sent[0]["content"] == expected_handler


@pytest.mark.asyncio
async def test_start_is_processed_as_a_regular_backend_turn() -> None:
    manager = FakeManager(make_channel())
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)

    task = await responder.handle_message(make_message(content="/start"))

    assert task is not None
    await task
    assert len(backend.turns) == 1
    assert backend.turns[0].message.content == "/start"
    assert backend.commands == []


@pytest.mark.asyncio
async def test_turn_response_uses_existing_outbound_manager() -> None:
    manager = FakeManager(make_channel())
    responder = CommunicationsResponder(manager, backend=RecordingBackend(turn_response="reply"))

    task = await responder.handle_message(make_message())
    assert task is not None
    await task

    assert manager.sent == [
        {
            "channel_name": "telegram",
            "content": "reply",
            "session_id": "session-1",
            "metadata": {"platform_destination": "chat-1"},
        }
    ]


@pytest.mark.asyncio
async def test_turn_adds_and_clears_configured_ack_reaction() -> None:
    manager = FakeManager(
        make_channel(
            config={
                "responder": {"enabled": True, "ack_reaction": "👀"},
                "allow_from": ["owner"],
            }
        )
    )
    responder = CommunicationsResponder(manager, backend=RecordingBackend())
    message = make_message()
    message.platform_message_id = "platform-message-1"

    task = await responder.handle_message(message)
    assert task is not None
    await task

    assert manager.reactions == [
        ("telegram", "chat-1", "platform-message-1", "👀"),
        ("telegram", "chat-1", "platform-message-1", None),
    ]


@pytest.mark.asyncio
async def test_reaction_event_never_starts_responder_turn() -> None:
    manager = FakeManager(make_channel())
    backend = RecordingBackend()
    responder = CommunicationsResponder(manager, backend=backend)
    message = make_message(content="👍")
    message.content_type = "reaction"

    task = await responder.handle_message(message)

    assert task is None
    assert backend.turns == []


@pytest.mark.asyncio
async def test_turn_queue_serializes_same_conversation() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    class BlockingBackend(RecordingBackend):
        async def run_turn(self, context: ResponderContext) -> str | None:
            self.turns.append(context)
            if context.message.content == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
            return None

    responder = CommunicationsResponder(FakeManager(make_channel()), backend=BlockingBackend())
    first = await responder.handle_message(make_message(content="first"))
    second = await responder.handle_message(make_message(content="second"))
    assert first is not None
    assert second is not None

    await first_started.wait()
    assert not second_started.is_set()

    release_first.set()
    await asyncio.gather(first, second)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_turn_queue_rejects_ninth_pending_turn_with_busy_response() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingBackend(RecordingBackend):
        async def run_turn(self, context: ResponderContext) -> str | None:
            self.turns.append(context)
            if context.message.content == "message-0":
                first_started.set()
                await release_first.wait()
            return None

    manager = FakeManager(make_channel())
    responder = CommunicationsResponder(manager, backend=BlockingBackend())
    accepted: list[asyncio.Task[None]] = []
    for index in range(8):
        task = await responder.handle_message(make_message(content=f"message-{index}"))
        assert task is not None
        accepted.append(task)

    await first_started.wait()
    rejected = await responder.handle_message(make_message(content="message-8"))

    assert rejected is None
    assert manager.sent[-1]["content"] == (
        "This conversation is busy. Try again after a pending response finishes."
    )

    release_first.set()
    await asyncio.gather(*accepted)


@pytest.mark.asyncio
async def test_turn_queue_runs_different_conversations_concurrently() -> None:
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    class BlockingBackend(RecordingBackend):
        async def run_turn(self, context: ResponderContext) -> str | None:
            started.add(context.conversation_id)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            return None

    responder = CommunicationsResponder(FakeManager(make_channel()), backend=BlockingBackend())
    first = await responder.handle_message(make_message(conversation_id="chat-1"))
    second = await responder.handle_message(make_message(conversation_id="chat-2"))
    assert first is not None
    assert second is not None

    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_comms_event_fans_out_to_websocket_and_responder() -> None:
    websocket = RecordingWebSocket()
    responder = RecordingEventResponder()
    manager = SimpleNamespace(event_callback=None, responder=responder)
    setup_communications_event_broadcasting(websocket, manager)
    message = make_message()

    await manager.event_callback("comms.message_received", message=message)

    assert websocket.events[0][0] == "comms.message_received"
    assert responder.events == [("comms.message_received", {"message": message})]


@pytest.mark.asyncio
async def test_responder_failure_does_not_break_websocket_event() -> None:
    websocket = RecordingWebSocket()
    responder = RecordingEventResponder(fail=True)
    manager = SimpleNamespace(event_callback=None, responder=responder)
    setup_communications_event_broadcasting(websocket, manager)

    await manager.event_callback("comms.message_received", message=make_message())

    assert websocket.events[0][0] == "comms.message_received"


@pytest.mark.asyncio
async def test_responder_receives_events_without_websocket() -> None:
    responder = RecordingEventResponder()
    manager = SimpleNamespace(event_callback=None, responder=responder)
    setup_communications_event_broadcasting(None, manager)
    message = make_message()

    await manager.event_callback("comms.message_received", message=message)

    assert responder.events == [("comms.message_received", {"message": message})]

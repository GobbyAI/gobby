"""The enrolled operator's DM belongs to terminal seats, never a chat responder."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.communications.operator_routing import reconcile_operator_target
from gobby.communications.telegram_actions import TelegramActionController
from gobby.communications.telegram_fallback import fallback_telegram_targets
from gobby.config.communications import CommunicationsConfig
from gobby.sessions.mailbox import MailboxService
from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.integration


@dataclass
class OperatorEnvironment:
    manager: CommunicationsManager
    sessions: SessionManager
    channel: ChannelConfig
    old: Session
    mailbox: MagicMock
    responder: MagicMock
    adapter: MagicMock
    project_id: str

    def assistant(self, definition: str = "assistant") -> Session:
        session = self.sessions.register("assistant-seat", None, "claude", self.project_id)
        SessionVariableManager(self.sessions.db).set_variable(session.id, "_agent_type", definition)
        return session

    def message(self) -> CommsMessage:
        return CommsMessage(
            id="",
            channel_id=self.channel.id,
            identity_id="42",
            direction="inbound",
            content="Please continue",
            created_at=utc_now(),
            metadata_json={"chat_id": "42", "conversation_type": "private"},
        )


@pytest.fixture
def operator_dm(temp_db: HubDatabase, sample_project: dict[str, str]) -> OperatorEnvironment:
    seed_seat(temp_db, "assistant", capabilities=["auto_attach_operator_conversations"])
    sessions = SessionManager(temp_db)
    project_id = sample_project["id"]
    old = sessions.register("expired-identity", None, "codex", project_id)
    sessions.update_status(old.id, "expired")
    store = LocalCommunicationsStore(temp_db)
    now = utc_now()
    channel = store.create_channel(
        ChannelConfig(
            id="",
            name="operator-telegram",
            channel_type="telegram",
            enabled=True,
            created_at=now,
            updated_at=now,
            config_json={
                "allow_from": ["42"],
                "operator_project_id": project_id,
                "responder": {"enabled": True, "project_id": project_id},
            },
        )
    )
    store.create_identity(
        CommsIdentity(
            id="",
            channel_id=channel.id,
            external_user_id="42",
            session_id=old.id,
            created_at=now,
            updated_at=now,
            metadata_json={"telegram_dm_conversation_id": "42"},
        )
    )
    manager = CommunicationsManager(CommunicationsConfig(), store, MagicMock(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = MagicMock()
    adapter.download_inbound_attachments = AsyncMock(return_value=[])
    adapter.send_message = AsyncMock(return_value="platform-1")
    manager._adapters[channel.name] = adapter
    mailbox = MagicMock()
    mailbox.send = AsyncMock(return_value=SimpleNamespace(success=True, wake_results=[]))
    manager.set_telegram_action_controller(TelegramActionController(manager, sessions, mailbox))
    backend = MagicMock()
    backend.run_turn = AsyncMock(return_value=None)
    manager.responder.set_backend(backend)
    manager.event_callback = manager.responder.handle_event
    return OperatorEnvironment(
        manager, sessions, channel, old, mailbox, backend, adapter, project_id
    )


@pytest.mark.parametrize("definition", ["assistant", "custom-inbox"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("granted", [False, True])
def test_automatic_operator_target_uses_definition_capability(
    operator_dm: OperatorEnvironment, definition: str, enabled: bool, granted: bool
) -> None:
    env = operator_dm
    definitions = AgentDefinitionManager(env.sessions.db)
    row = definitions.get_by_name("assistant")
    assert row is not None
    body = dict(row.definition_json)
    body["name"] = definition
    body["capabilities"] = ["auto_attach_operator_conversations"] if granted else []
    definitions.update(row.id, name=definition, definition_json=body, enabled=enabled)
    seat = env.assistant(definition)
    target = reconcile_operator_target(env.manager, env.channel)
    expected = seat.id if enabled and granted else None
    assert target == expected
    assert env.manager.attached_session(env.channel.id, "dm:42") == expected


@pytest.mark.asyncio
async def test_operator_dm_reaches_live_assistant_without_creating_chat(
    operator_dm: OperatorEnvironment,
) -> None:
    env = operator_dm
    assistant = env.assistant()
    before = {session.id for session in env.sessions.list()}
    messages = await env.manager.handle_inbound_messages(env.channel.name, [env.message()])
    await env.manager.responder.drain()
    assert messages[0].session_id == assistant.id
    assert env.manager.attached_session(env.channel.id, "dm:42") == assistant.id
    assert env.mailbox.send.await_count == 1
    assert env.mailbox.send.await_args.kwargs["target_id"] == assistant.id
    assert env.responder.run_turn.await_count == 0
    assert {session.id for session in env.sessions.list()} == before


@pytest.mark.asyncio
async def test_operator_waits_for_assistant_and_polling_drains_durable_queue(
    operator_dm: OperatorEnvironment,
) -> None:
    env = operator_dm
    before = {session.id for session in env.sessions.list()}
    messages = await env.manager.handle_inbound_messages(env.channel.name, [env.message()])
    await env.manager.responder.drain()
    assert env.responder.run_turn.await_count == 0
    assert env.mailbox.send.await_count == 0
    assert {session.id for session in env.sessions.list()} == before
    assert messages[0].session_id is None
    assistant = env.assistant()
    env.adapter.poll = AsyncMock(side_effect=[[], asyncio.CancelledError()])
    await env.manager._polling_manager._poll_loop(env.channel.name, env.adapter, 0)
    assert env.mailbox.send.await_count == 1
    assert env.mailbox.send.await_args.kwargs["target_id"] == assistant.id
    saved = env.manager.store.get_message(messages[0].id)
    assert saved is not None and saved.session_id == assistant.id
    env.adapter.poll = AsyncMock(side_effect=[[], asyncio.CancelledError()])
    await env.manager._polling_manager._poll_loop(env.channel.name, env.adapter, 0)
    assert env.mailbox.send.await_count == 1


def test_web_chat_cannot_take_operator_attachment(operator_dm: OperatorEnvironment) -> None:
    env = operator_dm
    web_chat = env.sessions.register(
        "fallback-chat", None, "codex", env.project_id, session_type="web_chat"
    )
    with pytest.raises(ValueError, match="terminal|non-comms"):
        env.manager.attach_conversation(env.channel.name, "dm:42", web_chat.id)
    assert env.manager.attached_session(env.channel.id, "dm:42") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", ["assistant", "custom-inbox"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("granted", [False, True])
async def test_deleted_telegram_target_falls_back_by_capability(
    operator_dm: OperatorEnvironment, definition: str, enabled: bool, granted: bool
) -> None:
    env = operator_dm
    definitions = AgentDefinitionManager(env.sessions.db)
    row = definitions.get_by_name("assistant")
    assert row is not None
    body = dict(row.definition_json)
    body["name"] = definition
    body["capabilities"] = ["auto_attach_operator_conversations"] if granted else []
    definitions.update(row.id, name=definition, definition_json=body, enabled=enabled)
    holder = env.sessions.register("selected-for-fallback", None, "codex", env.project_id)
    env.manager.attach_conversation(env.channel.name, "dm:42", holder.id)
    seat = env.assistant(definition)
    env.sessions.update_title(seat.id, "Assistant" if definition == "assistant" else "Help desk")
    assert env.sessions.delete(holder.id)

    await fallback_telegram_targets(
        env.manager, SessionStatusTransition.from_session(holder, status="deleted")
    )

    expected = seat.id if enabled and granted else None
    assert env.manager.attached_session(env.channel.id, "dm:42") == expected


@pytest.mark.asyncio
async def test_operator_expiry_rebinds_to_canonical_assistant_seat(
    operator_dm: OperatorEnvironment,
) -> None:
    env = operator_dm
    holder = env.sessions.register("selected-terminal", None, "codex", env.project_id)
    env.manager.attach_conversation(env.channel.name, "dm:42", holder.id)
    assistant = env.assistant()
    expired = env.sessions.update_status(holder.id, "expired")
    assert expired is not None
    await env.manager.handle_session_status_transition(
        SessionStatusTransition.from_session(expired)
    )
    assert env.manager.attached_session(env.channel.id, "dm:42") == assistant.id


@pytest.mark.asyncio
async def test_restart_replaces_persisted_web_chat_target(operator_dm: OperatorEnvironment) -> None:
    env = operator_dm
    fallback = env.sessions.register(
        "fallback", None, "codex", env.project_id, session_type="web_chat"
    )
    env.manager.store.set_telegram_agent_target(env.channel.id, "dm:42", fallback.id)
    channel = env.manager.store.get_channel(env.channel.id)
    assert channel is not None
    env.manager._channel_by_name[channel.name] = channel
    assistant = env.assistant()
    await env.manager._restore_telegram_targets(channel)
    assert env.manager.attached_session(channel.id, "dm:42") == assistant.id
    saved = env.manager.store.get_channel(channel.id)
    assert saved is not None
    assert saved.config_json["telegram_agent_targets"]["dm:42"] == assistant.id


@pytest.mark.asyncio
async def test_operator_delivery_failure_stays_queued(operator_dm: OperatorEnvironment) -> None:
    env = operator_dm
    assistant = env.assistant()
    env.mailbox.send.side_effect = [
        RuntimeError("mailbox unavailable"),
        SimpleNamespace(success=True, wake_results=[]),
    ]
    messages = await env.manager.handle_inbound_messages(env.channel.name, [env.message()])
    assert env.mailbox.send.await_count == 1
    env.adapter.poll = AsyncMock(side_effect=[[], asyncio.CancelledError()])
    await env.manager._polling_manager._poll_loop(env.channel.name, env.adapter, 0)
    assert env.mailbox.send.await_count == 2
    assert env.mailbox.send.await_args.kwargs["target_id"] == assistant.id
    saved = env.manager.store.get_message(messages[0].id)
    assert saved is not None and saved.session_id == assistant.id


@pytest.mark.asyncio
async def test_committed_operator_input_is_not_replayed_to_replacement(
    operator_dm: OperatorEnvironment,
) -> None:
    env = operator_dm
    assistant = env.assistant()
    mailbox_store = InterSessionMessageManager(env.sessions.db)
    mailbox = MailboxService(
        db=env.sessions.db, message_manager=mailbox_store, session_manager=env.sessions
    )
    env.manager.set_telegram_action_controller(
        TelegramActionController(env.manager, env.sessions, mailbox)
    )
    with patch.object(
        env.manager.store, "complete_operator_message", side_effect=RuntimeError("commit lost")
    ):
        messages = await env.manager.handle_inbound_messages(env.channel.name, [env.message()])
    assert len(mailbox_store.get_messages(assistant.id)) == 1
    env.sessions.update_status(assistant.id, "expired")
    replacement = env.sessions.register("replacement", None, "claude", env.project_id)
    SessionVariableManager(env.sessions.db).set_variable(replacement.id, "_agent_type", "assistant")
    env.adapter.poll = AsyncMock(side_effect=[[], asyncio.CancelledError()])
    await env.manager._polling_manager._poll_loop(env.channel.name, env.adapter, 0)
    assert mailbox_store.get_messages(replacement.id) == []
    saved = env.manager.store.get_message(messages[0].id)
    assert saved is not None and saved.session_id == assistant.id
    assert "operator_pending" not in saved.metadata_json

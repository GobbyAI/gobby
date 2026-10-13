"""Tests for CommunicationsManager."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.slack import SlackAdapter
from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.identities import IdentityResolution
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import (
    ChannelConfig,
    CommsAttachment,
    CommsIdentity,
    CommsMessage,
)
from gobby.communications.rate_limiter import RateLimitWaitExceeded
from gobby.communications.telegram_actions import TelegramActionController
from gobby.communications.voice import apply_voice_transcription
from gobby.config.communications import ChannelDefaults, CommunicationsConfig
from gobby.sessions.mailbox import MailboxSendResult
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager

_FIXED_TS = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment", [False, True])
async def test_operator_dm_clears_topic_thread_without_detaching(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    attachment: bool,
) -> None:
    from gobby.mcp_proxy.tools.communications import create_communications_registry
    from gobby.storage.definitions import AgentDefinitionManager
    from gobby.utils.machine_id import require_machine_id
    from gobby.workflows.state_manager import SessionVariableManager

    manager, channel, holder, adapter = operator_telegram
    if attachment:
        channel.config_json.pop("default_destination", None)
    caller = SessionManager(temp_db).register(
        external_id="topic-attached-assistant",
        machine_id=require_machine_id(),
        source="codex",
        project_id=str(sample_project["id"]),
        agent_depth=1,
    )
    agent_type = "archivist" if attachment else "assistant"
    AgentDefinitionManager(temp_db).create(
        name=agent_type,
        definition_json={
            "name": agent_type,
            "prompts": {"agent": "Assist the operator."},
            "workflows": {"rule_selectors": {"include": []}},
            "operator_message_channels": [channel.name],
        },
        source="custom",
    )
    SessionVariableManager(temp_db).set_variable(caller.id, "_agent_type", agent_type)
    holder.id = caller.id
    holder.transcript_path = None
    if not attachment:
        manager.attach_conversation(channel.name, "topic:-100123:77", caller.id)
        manager._thread_manager.track_thread(channel.id, caller.id, "77")
    manager._outbound.start()
    handler = create_communications_registry(manager, db=temp_db, workspace_root=tmp_path).get_tool(
        "send_operator_attachment" if attachment else "send_operator_message"
    )
    assert handler is not None
    try:
        with patch(
            "gobby.mcp_proxy.tools.communications.get_current_session_id", return_value=caller.id
        ):
            if attachment:
                report = tmp_path / "gobby-digest-2026-09-27.md"
                report.write_text("# Systems nominal\n")
                adapter.send_attachment = AsyncMock(return_value="operator-document-id")
                result = await handler(channel=channel.name, file_path=str(report))
            else:
                result = await handler(channel=channel.name, content="**Status**")
        assert result["success"] is True
        sent = (adapter.send_attachment if attachment else adapter.send_message).call_args.args[0]
        assert sent.session_id == caller.id
        assert sent.metadata_json["platform_destination"] == "42"
        assert sent.platform_thread_id is None
        if attachment:
            assert manager.attached_destination(channel.id, caller.id) is None
            return
        assert manager.attached_destination(channel.id, caller.id) == "topic:-100123:77"

        reply = await manager.send_message(channel.name, "Topic reply", session_id=caller.id)
        assert reply.status == "sent"
        assert reply.metadata_json["platform_destination"] == "-100123"
        assert reply.platform_thread_id == "77"
    finally:
        await manager._outbound.stop()


@pytest.fixture
def operator_telegram() -> tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock]:
    channel = make_channel(
        channel_type="telegram", config_json={"allow_from": ["42"], "default_destination": "88"}
    )
    store = make_store([channel])
    store.get_channel.return_value = channel
    identity = CommsIdentity(
        id="operator",
        channel_id=channel.id,
        external_user_id="42",
        session_id="comms-session",
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
        metadata_json={"conversation_reference": {"conversation_id": "42"}},
    )
    store.get_identity_by_external.return_value = identity
    store.update_identity.side_effect = lambda value: value
    sessions = MagicMock()
    holder = MagicMock(id="11111111-1111-4111-8111-111111111111", status="active", source="codex")
    sessions.get.side_effect = lambda session_id: holder if session_id == holder.id else None
    sessions.list.return_value = []
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    return manager, channel, holder, adapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_registration_sends_confirmation_and_replies_directly(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
) -> None:
    manager, channel, holder, adapter = operator_telegram
    resolved = manager.resolve_operator_conversation(channel.name)
    assert resolved == {
        "channel": channel.name,
        "conversation_id": "dm:42",
        "identity_id": "operator",
        "session_id": None,
    }
    cast(MagicMock, manager.store).list_identities.assert_not_called()
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    confirmation = await manager.send_message(channel.name, "Registered", session_id=holder.id)
    reply = await manager.send_message(channel.name, "Work complete", session_id=holder.id)
    assert [confirmation.status, reply.status] == ["sent", "sent"]
    assert [
        call.args[0].metadata_json["platform_destination"]
        for call in adapter.send_message.await_args_list
    ] == ["42", "42"]
    assert [call.args[0].session_id for call in adapter.send_message.await_args_list] == [
        holder.id,
        holder.id,
    ]
    assert manager.resolve_operator_conversation(channel.name)["session_id"] == holder.id
    assert manager.list_conversation_attachments() == [
        {"channel": channel.name, "conversation_id": "dm:42", "session_id": holder.id}
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to", [None, "confirmation-id"])
async def test_registered_operator_inbound_reaches_holder_mailbox(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
    reply_to: str | None,
) -> None:
    manager, channel, holder, _ = operator_telegram
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    source = CommsMessage(
        id="confirmation",
        channel_id=channel.id,
        direction="outbound",
        content="Registered",
        session_id=holder.id,
        created_at=_FIXED_TS,
    )
    cast(MagicMock, manager.store).get_message_by_platform_id.return_value = (
        source if reply_to else None
    )
    mailbox = MagicMock(send=AsyncMock(return_value=MailboxSendResult()))
    manager.set_telegram_action_controller(
        TelegramActionController(manager, manager._session_store, mailbox)
    )
    manager.event_callback = AsyncMock()
    inbound = CommsMessage(
        id="inbound",
        channel_id=channel.id,
        direction="inbound",
        content="Continue",
        identity_id="42",
        created_at=_FIXED_TS,
        metadata_json={
            "chat_id": "42",
            "conversation_type": "private",
            **({"reply_to_message_id": reply_to} if reply_to else {}),
        },
    )
    handled = await manager.handle_inbound_messages(channel.name, [inbound])
    assert handled[0].session_id == holder.id
    assert handled[0].metadata_json["session_route"] == ("reply" if reply_to else "attached")
    mailbox.send.assert_awaited_once()
    assert mailbox.send.await_args.kwargs["wake"] is True
    manager.event_callback.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("end_session", [False, True], ids=["explicit-detach", "session-end"])
async def test_operator_registration_releases_on_request_or_session_end(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
    end_session: bool,
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    manager, channel, holder, adapter = operator_telegram
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    if end_session:
        holder.status = "expired"
        await manager.handle_session_status_transition(
            SessionStatusTransition(
                session_id=holder.id,
                project_id="project-1",
                agent_run_id=None,
                status="expired",
                transitioned_at=_FIXED_TS,
                seq_num=1,
                title="Lane Developer",
                source="codex",
            )
        )
        adapter.send_message.assert_awaited_once()
        assert "No Assistant is running" in adapter.send_message.await_args.args[0].content
    else:
        manager.detach_conversation(channel.name, "dm:42", holder.id)
    assert (channel.id, "dm:42") not in manager._conversation_attachments
    assert manager.list_conversation_attachments() == []
    assert manager.attached_destination(channel.id, holder.id) is None
    assert manager.resolve_operator_conversation(channel.name)["session_id"] is None
    metadata = await manager._enrich_outbound_metadata(channel, channel.name, holder.id, None)
    assert metadata["platform_destination"] == "88"


@pytest.mark.unit
@pytest.mark.parametrize("allow_from", [[], ["42", "43"]])
def test_operator_resolution_rejects_missing_or_ambiguous_enrollment(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
    allow_from: list[str],
) -> None:
    manager, channel, _, _ = operator_telegram
    channel.config_json["allow_from"] = allow_from
    with pytest.raises(ValueError, match="exactly one enrolled operator"):
        manager.resolve_operator_conversation(channel.name)
    cast(MagicMock, manager.store).get_identity_by_external.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("chat_id", [None, "-10042", "0", "not-a-chat", 42])
def test_operator_resolution_rejects_unrecorded_dm_and_group_reference(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
    chat_id: object,
) -> None:
    manager, channel, _, _ = operator_telegram
    identity = manager.store.get_identity_by_external(channel.id, "42")
    assert identity is not None
    identity.metadata_json = {"conversation_reference": {"conversation_id": chat_id}}
    with pytest.raises(ValueError, match="no recorded Telegram DM"):
        manager.resolve_operator_conversation(channel.name)


@pytest.mark.unit
def test_operator_resolution_requires_linked_identity_and_telegram_channel(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
) -> None:
    manager, channel, _, _ = operator_telegram
    cast(MagicMock, manager.store).get_identity_by_external.return_value = None
    with pytest.raises(ValueError, match="no linked Telegram identity"):
        manager.resolve_operator_conversation(channel.name)
    channel.channel_type = "slack"
    with pytest.raises(ValueError, match="active Telegram channel"):
        manager.resolve_operator_conversation(channel.name)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_dm_is_remembered_after_group_identity_metadata_changes(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
) -> None:
    manager, channel, _, _ = operator_telegram
    message = CommsMessage(
        id="dm",
        channel_id=channel.id,
        direction="inbound",
        content="Hello",
        identity_id="42",
        created_at=_FIXED_TS,
        metadata_json={"chat_id": "42", "conversation_type": "private"},
    )
    handled = await manager.handle_inbound_messages(channel.name, [message])
    assert len(handled) == 1
    identity = manager.store.get_identity_by_external(channel.id, "42")
    assert identity is not None
    assert identity.metadata_json["telegram_dm_conversation_id"] == "42"
    manager._identity_manager.resolve_inbound_identity(
        channel.id,
        "42",
        metadata={"conversation_reference": {"conversation_id": "-10042"}},
        conversation_key="group:-10042",
    )
    assert identity.metadata_json["conversation_reference"]["conversation_id"] == "-10042"
    assert manager.resolve_operator_conversation(channel.name)["conversation_id"] == "dm:42"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["resume", "restart", "resume-after-restart"])
@pytest.mark.parametrize("reply_to", [None, "confirmation-id"])
async def test_registered_dm_survives_resume_and_daemon_restart(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    event: str,
    reply_to: str | None,
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    holder = sessions.register(
        external_id="telegram-resumable-holder",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        title="Assistant",
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(
        channel_type="telegram",
        channel_id=str(uuid.uuid4()),
        config_json={"allow_from": ["42"]},
    )
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    source = CommsMessage(
        id=str(uuid.uuid4()),
        channel_id=channel.id,
        direction="outbound",
        content="Registered",
        session_id=holder.id,
        platform_message_id="confirmation-id",
        metadata_json={"platform_destination": "42"},
        created_at=_FIXED_TS,
    )
    store.create_message(source)

    if event != "restart":
        assert sessions.update_session_status(holder.id, "expired")
        ended = sessions.get(holder.id)
        assert ended is not None
        await manager.handle_session_status_transition(SessionStatusTransition.from_session(ended))
        assert manager.attached_session(channel.id, "dm:42") is None
        adapter.send_message.assert_not_awaited()

    if event != "resume":
        # A new manager and fresh channel row represent a new daemon process.
        restored = store.get_channel(channel.id)
        assert restored is not None
        assert restored.config_json["telegram_agent_targets"] == {"dm:42": holder.id}
        manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)

        def check_target_before_polling(*args: object) -> None:
            assert manager._conversation_attachments[(channel.id, "dm:42")] == holder.id

        with (
            patch.object(manager._lifecycle, "init_adapter", AsyncMock(return_value=adapter)),
            patch.object(
                manager._lifecycle,
                "_start_polling_if_needed",
                side_effect=check_target_before_polling,
            ) as start_polling,
        ):
            await manager._lifecycle.activate_channel(restored)
        start_polling.assert_called_once()

    if event != "restart":
        resumed = sessions.revive_expired_terminal_session(holder.id)
        assert resumed is not None
        assert resumed.id == holder.id
        assert resumed.status == "active"
        # Delayed expiry events must not clear an already resumed holder.
        assert ended is not None
        await manager.handle_session_status_transition(SessionStatusTransition.from_session(ended))

    assert manager.attached_session(channel.id, "dm:42") == holder.id
    mailbox = MagicMock(send=AsyncMock(return_value=MailboxSendResult()))
    manager.set_telegram_action_controller(TelegramActionController(manager, sessions, mailbox))
    manager.event_callback = AsyncMock()
    inbound = CommsMessage(
        id=str(uuid.uuid4()),
        channel_id=channel.id,
        direction="inbound",
        content="Continue after resume",
        identity_id="42",
        created_at=_FIXED_TS,
        metadata_json={
            "chat_id": "42",
            "conversation_type": "private",
            **({"reply_to_message_id": reply_to} if reply_to else {}),
        },
    )
    handled = await manager.handle_inbound_messages(channel.name, [inbound])
    assert handled[0].session_id == holder.id
    assert handled[0].metadata_json["session_route"] == ("reply" if reply_to else "attached")
    mailbox.send.assert_awaited_once()
    manager.event_callback.assert_not_awaited()
    adapter.send_message.assert_not_awaited()

    manager.detach_conversation(channel.name, "dm:42", holder.id)
    restored = store.get_channel(channel.id)
    assert restored is not None
    assert restored.config_json["telegram_agent_targets"] == {}
    restarted = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    restarted._channel_by_name[channel.name] = restored
    await restarted._restore_telegram_targets(restored)
    assert restarted.attached_session(channel.id, "dm:42") is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("assistant_available", [False, True])
@pytest.mark.parametrize("hard_delete", [False, True])
async def test_restore_releases_target_deleted_while_daemon_was_down(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    assistant_available: bool,
    hard_delete: bool,
) -> None:
    """Restore reconciles offline deletion before the channel starts polling."""
    sessions = SessionManager(temp_db)
    holder = sessions.register(
        external_id="offline-deleted-target",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        title="Lane Developer",
    )
    assistant = (
        sessions.register(
            external_id="restore-assistant",
            machine_id=None,
            source="claude",
            project_id=sample_project["id"],
            title="Assistant",
        )
        if assistant_available
        else None
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(
        channel_type="telegram",
        channel_id=str(uuid.uuid4()),
        config_json={"allow_from": ["42"]},
    )
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    # No status listener is running while the daemon is down.
    if hard_delete:
        assert sessions.delete(holder.id)
        assert sessions.get(holder.id) is None
    else:
        assert sessions.update_session_status(holder.id, "deleted")
    restored = store.get_channel(channel.id)
    assert restored is not None
    restarted = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    adapter = make_adapter(channel_type="telegram")
    expected = assistant.id if assistant is not None else None

    def check_target_before_polling(*args: object) -> None:
        assert restarted._conversation_attachments.get((channel.id, "dm:42")) == expected

    with (
        patch.object(restarted._lifecycle, "init_adapter", AsyncMock(return_value=adapter)),
        patch.object(
            restarted._lifecycle,
            "_start_polling_if_needed",
            side_effect=check_target_before_polling,
        ) as start_polling,
    ):
        await restarted._lifecycle.activate_channel(restored)

    start_polling.assert_called_once()
    assert restarted.attached_session(channel.id, "dm:42") == expected
    persisted = store.get_channel(channel.id)
    assert persisted is not None
    assert persisted.config_json["telegram_agent_targets"] == (
        {"dm:42": expected} if assistant_available else {}
    )
    if assistant_available:
        adapter.send_message.assert_not_awaited()
    else:
        adapter.send_message.assert_awaited_once()
        assert "No Assistant is running" in adapter.send_message.await_args.args[0].content


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["delete", "prune"])
@pytest.mark.parametrize("assistant_available", [False, True])
async def test_hard_deleted_session_releases_telegram_target_after_commit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    deletion: str,
    assistant_available: bool,
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    holder = sessions.register(
        external_id="hard-deleted-target",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        title="Lane Developer",
    )
    assistant = (
        sessions.register(
            external_id="delete-target-assistant",
            machine_id=None,
            source="claude",
            project_id=sample_project["id"],
            title="Assistant",
        )
        if assistant_available
        else None
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_type="telegram", channel_id=str(uuid.uuid4()))
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    if deletion == "prune":
        sessions.update_status(holder.id, "expired")
        temp_db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE id = %s",
            (holder.id,),
        )
    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)
    with temp_db.transaction():
        if deletion == "delete":
            assert sessions.delete(holder.id)
        else:
            assert sessions.prune_empty_sessions() == 1
        assert sessions.get(holder.id) is None
        assert transitions == []
    assert len(transitions) == 1
    transition = transitions[0]
    assert (transition.session_id, transition.project_id, transition.status) == (
        holder.id,
        holder.project_id,
        "deleted",
    )
    assert transition.seq_num == holder.seq_num
    await manager.handle_session_status_transition(transition)
    expected = assistant.id if assistant is not None else None
    assert manager.attached_session(channel.id, "dm:42") == expected
    persisted = store.get_channel(channel.id)
    assert persisted is not None
    assert persisted.config_json["telegram_agent_targets"] == (
        {"dm:42": expected} if assistant_available else {}
    )
    if assistant_available:
        adapter.send_message.assert_not_awaited()
    else:
        adapter.send_message.assert_awaited_once()
        assert "No Assistant is running" in adapter.send_message.await_args.args[0].content


@pytest.fixture
def _legacy_identity_index(temp_db: HubDatabase, deletion: str) -> Iterator[None]:
    if deletion != "identity":
        yield
        return
    row = temp_db.fetchone("SELECT pg_get_indexdef('idx_sessions_unique'::regclass) AS definition")
    assert row is not None
    definition = row["definition"]
    try:
        yield
    finally:
        # A rollback leaves the duplicate present; make its identity unique first.
        temp_db.execute(
            "UPDATE sessions SET external_id = id::text "
            "WHERE external_id = 'legacy-canonical' AND message_count = 0"
        )
        temp_db.execute(definition)


def _legacy_delete_target(
    sessions: SessionManager, project_id: str, deletion: str
) -> tuple[Session, Session]:
    context = {"parent_pid": 4242, "parent_create_time": 1234.5}
    canonical = sessions.register(
        external_id="legacy-canonical",
        machine_id=None,
        source="claude",
        project_id=project_id,
        terminal_context=context,
    )
    holder = sessions.register(
        external_id="legacy-holder",
        machine_id=None,
        source="claude",
        project_id=project_id,
        terminal_context=context,
        title="Lane Developer",
    )
    sessions.db.execute("UPDATE sessions SET message_count = 1 WHERE id = %s", (canonical.id,))
    if deletion == "identity":
        # Recreate a pre-unique-index collision only in the isolated test schema.
        with sessions.db.transaction() as conn:
            conn.execute("DROP INDEX idx_sessions_unique")
            conn.execute(
                "UPDATE sessions SET external_id = %s WHERE id = %s",
                (canonical.external_id, holder.id),
            )
    return canonical, holder


def _delete_legacy_target(sessions: SessionManager, canonical: Session, deletion: str) -> None:
    if deletion == "compact":
        from gobby.storage.session_activity import reconcile_compact_session_activity

        result = reconcile_compact_session_activity(sessions, canonical.id)
        assert result.success
        assert len(result.deleted_ghost_ids) == 1
    else:
        result_session = sessions.register(
            external_id=canonical.external_id,
            machine_id=None,
            source=canonical.source,
            project_id=canonical.project_id,
        )
        assert result_session.id == canonical.id


@pytest.mark.integration
@pytest.mark.parametrize("deletion", ["identity"])
@pytest.mark.parametrize("observation", ["cache", "event", "rollback"])
@pytest.mark.usefixtures("_legacy_identity_index")
def test_identity_delete_invalidates_caches_and_emits_committed_change(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    deletion: str,
    observation: str,
) -> None:
    sessions = SessionManager(temp_db)
    canonical, holder = _legacy_delete_target(sessions, sample_project["id"], deletion)
    sessions.cache_session_mapping(
        external_id=canonical.external_id,
        session_id=holder.id,
        source=holder.source,
        project_id=holder.project_id,
        session_type=holder.session_type,
    )
    sessions._session_metadata[holder.id] = {"title": holder.title}
    key = (canonical.external_id, holder.source, holder.project_id, holder.session_type)
    assert sessions._session_mapping[key] == holder.id
    assert key in sessions._session_mapping_timestamps
    changes: list[tuple[str, str]] = []

    def record_change(event: str, session_id: str) -> None:
        changes.append((event, session_id))

    sessions.register_session_change_listener(record_change)
    if observation == "rollback":
        with pytest.raises(RuntimeError, match="rollback identity collapse"):
            with temp_db.transaction():
                _delete_legacy_target(sessions, canonical, deletion)
                assert changes == []
                raise RuntimeError("rollback identity collapse")
        assert sessions.get(holder.id) is not None
        assert changes == []
        return

    with temp_db.transaction():
        _delete_legacy_target(sessions, canonical, deletion)
        assert changes == []
    assert sessions.get(holder.id) is None
    assert sessions.get(canonical.id) is not None
    if observation == "cache":
        assert holder.id not in sessions._session_mapping.values()
        assert key not in sessions._session_mapping_timestamps
        assert holder.id not in sessions._session_metadata
    else:
        assert changes == [("session_deleted", holder.id), ("session_updated", canonical.id)]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["compact", "identity"])
@pytest.mark.parametrize("assistant_available", [False, True])
async def test_legacy_delete_releases_telegram_target_after_commit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    deletion: str,
    assistant_available: bool,
    _legacy_identity_index: None,
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    canonical, holder = _legacy_delete_target(sessions, sample_project["id"], deletion)
    sessions.cache_session_mapping(
        external_id=holder.external_id,
        source=holder.source,
        session_id=holder.id,
        project_id=holder.project_id,
        session_type=holder.session_type,
    )
    sessions._session_metadata[holder.id] = {"title": holder.title}
    key = (holder.external_id, holder.source, holder.project_id, holder.session_type)
    assert sessions._session_mapping[key] == holder.id
    assert key in sessions._session_mapping_timestamps
    changes: list[tuple[str, str]] = []

    def record_change(event: str, session_id: str) -> None:
        changes.append((event, session_id))

    assistant = (
        sessions.register(
            external_id="legacy-assistant",
            machine_id=None,
            source="claude",
            project_id=sample_project["id"],
            title="Assistant",
        )
        if assistant_available
        else None
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_type="telegram", channel_id=str(uuid.uuid4()))
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)
    sessions.register_session_change_listener(record_change)
    with temp_db.transaction():
        _delete_legacy_target(sessions, canonical, deletion)
        assert sessions.get(holder.id) is None
        assert transitions == []
        assert changes == []
    assert holder.id not in sessions._session_mapping.values()
    assert key not in sessions._session_mapping_timestamps
    assert holder.id not in sessions._session_metadata
    assert [event for event in changes if event[0] == "session_deleted"] == [
        ("session_deleted", holder.id)
    ]
    deleted = [event for event in transitions if event.status == "deleted"]
    assert len(deleted) == 1
    assert (deleted[0].session_id, deleted[0].project_id, deleted[0].seq_num) == (
        holder.id,
        holder.project_id,
        holder.seq_num,
    )
    for event in transitions:
        await manager.handle_session_status_transition(event)
    expected = assistant.id if assistant is not None else None
    assert manager.attached_session(channel.id, "dm:42") == expected
    persisted = store.get_channel(channel.id)
    assert persisted is not None
    assert persisted.config_json["telegram_agent_targets"] == (
        {"dm:42": expected} if assistant_available else {}
    )
    if assistant_available:
        adapter.send_message.assert_not_awaited()
    else:
        adapter.send_message.assert_awaited_once()
        assert "No Assistant is running" in adapter.send_message.await_args.args[0].content


@pytest.mark.integration
@pytest.mark.parametrize("deletion", ["compact", "identity"])
def test_legacy_delete_rollback_does_not_emit_end_event(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    deletion: str,
    _legacy_identity_index: None,
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    canonical, holder = _legacy_delete_target(sessions, sample_project["id"], deletion)
    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)
    with pytest.raises(RuntimeError, match="roll back deletion"):
        with temp_db.transaction():
            _delete_legacy_target(sessions, canonical, deletion)
            raise RuntimeError("roll back deletion")
    assert sessions.get(holder.id) is not None
    assert transitions == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_offline_deleted_restore_prefers_same_project_assistant(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition
    from gobby.storage.projects import LocalProjectManager

    sessions = SessionManager(temp_db)
    same_project = sessions.register(
        external_id="same-project-assistant",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        title="Assistant",
    )
    other_project = LocalProjectManager(temp_db).create(name="Other")
    other = sessions.register(
        external_id="other-project-assistant",
        machine_id=None,
        source="claude",
        project_id=other_project.id,
        title="Assistant",
    )
    # list() returns the newer foreign Assistant first; project preference must win.
    assert sessions.list(statuses=["active"])[0].id == other.id
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(
        channel_type="telegram",
        channel_id=str(uuid.uuid4()),
        config_json={"telegram_agent_targets": {"dm:42": str(uuid.uuid4())}},
    )
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    transitions: list[SessionStatusTransition] = []
    original = manager.handle_session_status_transition

    async def record_transition(transition: SessionStatusTransition) -> None:
        transitions.append(transition)
        await original(transition)

    with patch.object(manager, "handle_session_status_transition", side_effect=record_transition):
        await manager._restore_telegram_targets(channel)
    assert len(transitions) == 1
    assert transitions[0].project_id == sample_project["id"]
    assert manager.attached_session(channel.id, "dm:42") == same_project.id
    persisted = store.get_channel(channel.id)
    assert persisted is not None
    assert persisted.config_json["telegram_agent_targets"] == {"dm:42": same_project.id}


@pytest.mark.integration
@pytest.mark.parametrize("deletion", ["delete", "prune"])
def test_hard_deleted_session_rollback_does_not_emit_end_event(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    deletion: str,
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    holder = sessions.register(
        external_id="rolled-back-delete",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
    )
    if deletion == "prune":
        sessions.update_status(holder.id, "expired")
        temp_db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE id = %s",
            (holder.id,),
        )
    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)
    with pytest.raises(RuntimeError, match="roll back deletion"):
        with temp_db.transaction():
            if deletion == "delete":
                assert sessions.delete(holder.id)
            else:
                assert sessions.prune_empty_sessions() == 1
            raise RuntimeError("roll back deletion")
    assert sessions.get(holder.id) is not None
    assert transitions == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_lookup_failure_still_starts_polling(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
) -> None:
    manager, channel, holder, adapter = operator_telegram
    channel.config_json["telegram_agent_targets"] = {"dm:42": holder.id, "dm:43": "other"}
    manager._channel_init_errors[channel.name] = "previous startup error"

    def lookup(session_id: str) -> None:
        if session_id == holder.id:
            raise RuntimeError("temporary session lookup failure")

    with (
        patch.object(manager._session_store, "get", side_effect=lookup) as get_session,
        patch.object(manager._lifecycle, "init_adapter", AsyncMock(return_value=adapter)),
        patch.object(manager._lifecycle, "_start_polling_if_needed") as start_polling,
    ):
        assert await manager._lifecycle.activate_channel(channel) is adapter

    get_session.assert_any_call(holder.id)
    get_session.assert_any_call("other")
    start_polling.assert_called_once_with(channel.name, channel, adapter)
    assert channel.name not in manager._channel_init_errors
    assert manager._adapters[channel.name] is adapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_during_fallback_does_not_clear_target_or_send_notice(
    operator_telegram: tuple[CommunicationsManager, ChannelConfig, MagicMock, MagicMock],
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    manager, channel, holder, adapter = operator_telegram
    holder.agent_run_id = "managed-run"
    manager.attach_conversation(channel.name, "dm:42", holder.id)
    holder.status = "expired"

    def resume_during_lookup(**kwargs: object) -> list[object]:
        holder.status = "active"
        return []

    cast(MagicMock, manager._session_store).list.side_effect = resume_during_lookup
    await manager.handle_session_status_transition(
        SessionStatusTransition(
            session_id=holder.id,
            project_id="project-1",
            agent_run_id="managed-run",
            status="expired",
            transitioned_at=_FIXED_TS,
            seq_num=1,
            title="Assistant",
            source="codex",
        )
    )
    assert manager.attached_session(channel.id, "dm:42") == holder.id
    adapter.send_message.assert_not_awaited()
    cast(MagicMock, manager.store).set_telegram_agent_target.assert_called_once_with(
        channel.id, "dm:42", holder.id
    )


def make_config() -> CommunicationsConfig:
    return CommunicationsConfig(
        enabled=True,
        channel_defaults=ChannelDefaults(rate_limit_per_minute=60, burst=10),
    )


def make_channel(
    name: str = "test-channel",
    channel_type: str = "test",
    channel_id: str = "chan-1",
    enabled: bool = True,
    config_json: dict[str, Any] | None = None,
    webhook_secret: str | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        channel_type=channel_type,
        name=name,
        enabled=enabled,
        config_json=config_json or {},
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
        webhook_secret=webhook_secret,
    )


def make_store(channels: list[ChannelConfig] | None = None) -> MagicMock:
    store = MagicMock()
    stored_channels = channels or []
    store.list_channels.return_value = stored_channels
    store.get_channel_by_name.side_effect = lambda name: next(
        (channel for channel in store.list_channels.return_value if channel.name == name),
        None,
    )

    def update_channel(updated: ChannelConfig) -> ChannelConfig:
        store.list_channels.return_value = [
            updated if channel.id == updated.id else channel
            for channel in store.list_channels.return_value
        ]
        return updated

    store.update_channel.side_effect = update_channel
    store.create_message.side_effect = lambda message: message

    def create_message_with_attachments(
        message: CommsMessage,
        attachments: list[CommsAttachment],
    ) -> tuple[CommsMessage, list[CommsAttachment]]:
        for attachment in attachments:
            attachment.message_id = message.id
        return message, attachments

    store.create_message_with_attachments.side_effect = create_message_with_attachments
    store.create_channel.return_value = None
    store.get_message_by_platform_id.return_value = None

    def delete_channel(channel_id: str) -> None:
        store.list_channels.return_value = [
            channel for channel in store.list_channels.return_value if channel.id != channel_id
        ]

    store.delete_channel.side_effect = delete_channel
    store.get_identity_by_external.return_value = None
    return store


def make_secret_store() -> MagicMock:
    secret_store = MagicMock()
    secret_store.get.return_value = None
    return secret_store


def make_adapter(
    channel_type: str = "test",
    supports_webhooks: bool = True,
    supports_polling: bool = False,
) -> MagicMock:
    adapter = MagicMock()
    adapter.channel_type = channel_type
    adapter.supports_webhooks = supports_webhooks
    adapter.supports_polling = supports_polling
    adapter.initialize = AsyncMock()
    adapter.send_message = AsyncMock(return_value="platform-msg-id-1")
    adapter.send_proactive = AsyncMock(return_value="platform-proactive-id-1")
    adapter.shutdown = AsyncMock()
    adapter.parse_webhook.return_value = []
    adapter.download_inbound_attachments = AsyncMock(return_value=[])
    adapter.acknowledge_webhook_messages = AsyncMock()
    adapter.verify_webhook.return_value = True
    return adapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_loads_channels() -> None:
    """start() loads enabled channels and initializes adapters."""
    channel = make_channel()
    store = make_store([channel])
    secret_store = make_secret_store()
    config = make_config()

    manager = CommunicationsManager(config, store, secret_store, MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    assert "test-channel" in manager._adapters
    mock_adapter.initialize.assert_called_once()
    # start() lists channels for gobby_chat creation, then enabled channels for activation.
    assert store.list_channels.call_count == 2
    store.list_channels.assert_any_call(enabled_only=False)
    store.list_channels.assert_any_call(enabled_only=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_polls_poll_only_adapter_with_global_webhook_url() -> None:
    """Poll-only adapters keep polling when webhook-capable channels use webhooks."""
    channel = make_channel()
    store = make_store([channel])
    config = make_config()
    config.webhook_base_url = "https://example.com"
    manager = CommunicationsManager(config, store, make_secret_store(), MagicMock())
    start_polling = MagicMock()

    adapter = make_adapter(supports_webhooks=False, supports_polling=True)
    adapter_cls = MagicMock(return_value=adapter)

    with (
        patch.object(manager._polling_manager, "start_polling", start_polling),
        patch("gobby.communications.manager.get_adapter_class", return_value=adapter_cls),
    ):
        await manager.start()

    assert manager._adapters[channel.name] is adapter
    assert manager._channel_by_name[channel.name] is channel
    start_polling.assert_called_once_with(channel.name, adapter, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_config_updates_persist_through_channel_store() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={"bot_token": "$secret:telegram-token"},
    )
    store = make_store([channel])
    store.merge_channel_config.return_value = {
        "bot_token": "$secret:telegram-token",
        "poll_offset": 501,
    }
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter(channel_type="telegram")
    adapter_cls = MagicMock(return_value=adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=adapter_cls):
        await manager._init_adapter(channel)

    persist_config = adapter.set_config_update_callback.call_args.args[0]
    await persist_config({"poll_offset": 501})

    assert channel.config_json == {
        "bot_token": "$secret:telegram-token",
        "poll_offset": 501,
    }
    store.merge_channel_config.assert_called_once_with(channel.id, {"poll_offset": 501})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_adapter_config_callback_cannot_overwrite_replacement() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={"bot_token": "$secret:telegram-token"},
    )
    store = make_store([channel])
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    first_adapter = make_adapter(channel_type="telegram")
    second_adapter = make_adapter(channel_type="telegram")
    adapter_cls = MagicMock(side_effect=[first_adapter, second_adapter])

    with patch("gobby.communications.manager.get_adapter_class", return_value=adapter_cls):
        await manager._init_adapter(channel)
        stale_callback = first_adapter.set_config_update_callback.call_args.args[0]
        manager._adapters[channel.name] = first_adapter
        manager._channel_by_name[channel.name] = channel
        await manager.update_channel(channel)

    store.update_channel.reset_mock()
    await stale_callback({"poll_offset": 100})

    store.update_channel.assert_not_called()
    assert channel.config_json == {"bot_token": "$secret:telegram-token"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_telegram_group_message_skips_telegram_group_policy() -> None:
    channel = make_channel(channel_type="slack")
    manager = CommunicationsManager(
        make_config(),
        make_store([channel]),
        make_secret_store(),
        MagicMock(),
    )
    message = CommsMessage(
        id="message-1",
        channel_id=channel.id,
        direction="inbound",
        content="hello group",
        metadata_json={"is_group": True},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with patch(
        "gobby.communications.manager.evaluate_group_message",
        side_effect=AssertionError("telegram-only policy was called"),
    ):
        admitted = await manager.admit_inbound_message(channel, message)

    assert admitted is True


@pytest.mark.unit
async def test_telegram_init_uses_global_webhook_url_as_inbound_source() -> None:
    """Telegram webhook setup follows the manager's global inbound mode decision."""
    channel = make_channel(channel_type="telegram", config_json={})
    store = make_store([channel])
    config = make_config()
    config.webhook_base_url = "https://global.example/hooks"
    manager = CommunicationsManager(config, store, make_secret_store(), MagicMock())
    start_polling = MagicMock()

    adapter = make_adapter(channel_type="telegram", supports_webhooks=True, supports_polling=True)

    with (
        patch.object(manager._polling_manager, "start_polling", start_polling),
        patch(
            "gobby.communications.manager.get_adapter_class",
            return_value=MagicMock(return_value=adapter),
        ),
    ):
        await manager.start()

    init_channel = adapter.initialize.call_args.args[0]
    assert manager._adapters[channel.name] is adapter
    assert "webhook_base_url" not in channel.config_json
    assert init_channel.config_json["webhook_base_url"] == "https://global.example/hooks"
    start_polling.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_init_resolves_webhook_secret_reference() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={},
        webhook_secret="$secret:COMMS_TELEGRAM_WEBHOOK_SECRET_MY_TELEGRAM",
    )
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = "resolved-webhook-secret"
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())
    adapter = make_adapter(channel_type="telegram", supports_webhooks=True, supports_polling=True)

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    init_channel = adapter.initialize.call_args.args[0]
    assert channel.webhook_secret == "$secret:COMMS_TELEGRAM_WEBHOOK_SECRET_MY_TELEGRAM"
    assert init_channel.webhook_secret == "resolved-webhook-secret"
    secret_store.get.assert_called_once_with("COMMS_TELEGRAM_WEBHOOK_SECRET_MY_TELEGRAM")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_init_removes_stale_channel_webhook_url_when_polling() -> None:
    """Telegram polling setup cannot leave a channel-level webhook registered."""
    channel = make_channel(
        channel_type="telegram",
        config_json={"webhook_base_url": "https://stale.example/hooks"},
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    start_polling = MagicMock()

    adapter = make_adapter(channel_type="telegram", supports_webhooks=True, supports_polling=True)

    with (
        patch.object(manager._polling_manager, "start_polling", start_polling),
        patch(
            "gobby.communications.manager.get_adapter_class",
            return_value=MagicMock(return_value=adapter),
        ),
    ):
        await manager.start()

    init_channel = adapter.initialize.call_args.args[0]
    assert manager._adapters[channel.name] is adapter
    assert channel.config_json["webhook_base_url"] == "https://stale.example/hooks"
    assert "webhook_base_url" not in init_channel.config_json
    start_polling.assert_called_once_with(channel.name, adapter, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_skips_unknown_adapter() -> None:
    """start() logs error but continues if adapter type is unknown."""
    channel = make_channel(channel_type="unknown_type")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        await manager.start()

    assert "test-channel" not in manager._adapters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_rejects_invalid_channel_rate_limit_before_activation() -> None:
    channel = make_channel(config_json={"rate_limit_per_minute": 0, "burst": 1})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter_cls = MagicMock(return_value=make_adapter())

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    assert "test-channel" not in manager._adapters
    mock_adapter_cls.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_shuts_down_all_adapters() -> None:
    """stop() calls shutdown on all active adapters and clears state."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    events: list[str] = []

    mock_adapter = make_adapter()
    mock_adapter.shutdown.side_effect = lambda: events.append("adapter")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    vision_service = MagicMock()
    vision_service.stop = AsyncMock(side_effect=lambda: events.append("vision"))
    manager.set_vision_extract_service(vision_service)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    responder_stop = AsyncMock(side_effect=lambda: events.append("responder"))
    with patch.object(manager.responder, "stop", responder_stop):
        await manager.stop()

    mock_adapter.shutdown.assert_called_once()
    vision_service.stop.assert_awaited_once_with()
    assert events == ["responder", "adapter", "vision"]
    assert len(manager._adapters) == 0
    assert len(manager._channel_by_name) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_success() -> None:
    """send_message() sends and stores message, returns CommsMessage."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert msg.content == "Hello!"
    assert msg.direction == "outbound"
    assert msg.status == "sent"
    assert msg.platform_message_id == "platform-msg-id-1"
    mock_adapter.send_message.assert_called_once()
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_drains_in_flight_send_before_transport_close() -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    entered = asyncio.Event()
    released = asyncio.Event()
    transport_closed = False

    async def send(message: CommsMessage) -> str:
        entered.set()
        await released.wait()
        if transport_closed:
            raise httpx.ReadError("")
        return "delivered-before-close"

    async def close() -> None:
        nonlocal transport_closed
        transport_closed = True

    adapter.send_message.side_effect = send
    adapter.shutdown.side_effect = close
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()
    sending = asyncio.create_task(manager.send_message("test-channel", "shutdown response"))
    await entered.wait()
    with patch.object(manager.responder, "stop", AsyncMock(side_effect=released.set)):
        await manager.stop()
    message = await sending

    assert message.status == "sent"
    assert message.platform_message_id == "delivered-before-close"
    assert transport_closed
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "attachment", "proactive"])
async def test_stop_deadline_keeps_durable_send_and_logs_id(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    kind: str,
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    entered = asyncio.Event()
    never = asyncio.Event()
    messages: list[CommsMessage] = []

    def reserve(message: CommsMessage) -> CommsMessage:
        messages.append(message)
        return message

    store.create_message.side_effect = reserve

    async def send(*args: object) -> str:
        entered.set()
        await never.wait()
        return "unreachable"

    adapter.send_message.side_effect = send
    adapter.send_attachment = AsyncMock(side_effect=send)
    adapter.send_proactive.side_effect = send
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    async def deliver() -> CommsMessage:
        if kind == "attachment":
            file_path = tmp_path / "deadline.txt"
            file_path.write_text("deadline file")
            message, _ = await manager.send_attachment("test-channel", file_path)
            return message
        if kind == "proactive":
            return await manager.send_proactive("test-channel", "destination", "deadline response")
        return await manager.send_message("test-channel", "deadline response")

    sending = asyncio.create_task(deliver())
    await entered.wait()
    with patch("gobby.communications.outbound._OUTBOUND_DRAIN_SECONDS", 0):
        await manager.stop()
    with pytest.raises(asyncio.CancelledError):
        await sending

    store.create_message.assert_called_once()
    store.update_message_delivery.assert_called_once()
    assert messages[0].status == "failed"
    assert messages[0].id in caplog.text
    assert "CancelledError" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "stage"),
    [(kind, stage) for kind in ("text", "attachment") for stage in ("session", "metadata")],
)
async def test_shutdown_rejects_unadmitted_send(tmp_path: Path, kind: str, stage: str) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    adapter.send_attachment = AsyncMock(return_value="attachment-id")
    entered = asyncio.Event()
    released = asyncio.Event()

    async def prepare(*args: object) -> dict[str, object]:
        entered.set()
        await released.wait()
        return {}

    file_path = tmp_path / "preparing.txt"
    file_path.write_text("preparing attachment")
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()
    method = {"session": "_require_session", "metadata": "enrich_metadata"}[stage]

    async def deliver() -> None:
        if kind == "attachment":
            await manager.send_attachment("test-channel", file_path)
        else:
            await manager.send_message("test-channel", "preparing")

    with patch.object(manager._outbound, method, AsyncMock(side_effect=prepare)):
        sending = asyncio.create_task(deliver())
        await entered.wait()
        with patch("gobby.communications.outbound._OUTBOUND_DRAIN_SECONDS", 0):
            await manager.stop()
        released.set()
        with pytest.raises(RuntimeError, match="stopping"):
            await sending

    adapter.send_message.assert_not_awaited()
    adapter.send_attachment.assert_not_awaited()
    adapter.send_proactive.assert_not_awaited()
    store.create_message.assert_not_called()
    assert not sending.cancelled()
    assert not manager._outbound._pending


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "attachment", "proactive"])
@pytest.mark.parametrize("deadline", [False, True])
async def test_cancelled_caller_during_reservation_is_settled(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, kind: str, deadline: bool
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    adapter.send_attachment = AsyncMock(return_value="attachment-id")
    entered = asyncio.Event()
    inserted = asyncio.Event()
    released = threading.Event()
    loop = asyncio.get_running_loop()
    rows: list[CommsMessage] = []

    def reserve(message: CommsMessage) -> CommsMessage:
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(30), "test did not release the reservation insert"
        rows.append(message)
        loop.call_soon_threadsafe(inserted.set)
        return message

    store.create_message.side_effect = reserve
    file_path = tmp_path / "reserving.txt"
    file_path.write_text("reservation attachment")
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    async def deliver() -> None:
        if kind == "attachment":
            await manager.send_attachment("test-channel", file_path)
        elif kind == "proactive":
            await manager.send_proactive("test-channel", "dest", "reserving")
        else:
            await manager.send_message("test-channel", "reserving")

    sending = asyncio.create_task(deliver())
    try:
        await entered.wait()
        pending = tuple(manager._outbound._pending)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        if deadline:
            with patch("gobby.communications.outbound._OUTBOUND_DRAIN_SECONDS", 0):
                await manager.stop()
            released.set()
        else:
            with patch.object(manager.responder, "stop", AsyncMock(side_effect=released.set)):
                await manager.stop()
        await inserted.wait()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        store.update_message_delivery.assert_called_once()
        expected = "failed" if deadline else "sent"
        assert rows[0].status == expected
        assert store.update_message_delivery.call_args.args[1] == expected
        if deadline:
            assert rows[0].id in caplog.text
            assert "CancelledError" in caplog.text
            adapter.send_message.assert_not_awaited()
            adapter.send_attachment.assert_not_awaited()
            adapter.send_proactive.assert_not_awaited()
        else:
            assert rows[0].platform_message_id is not None
    finally:
        released.set()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "attachment", "proactive"])
async def test_empty_outbound_error_log_names_type_and_id(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    kind: str,
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    adapter.send_message.side_effect = httpx.ReadError("")
    adapter.send_attachment = AsyncMock(side_effect=httpx.ReadError(""))
    adapter.send_proactive.side_effect = httpx.ReadError("")
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()
    if kind == "attachment":
        file_path = tmp_path / "diagnostic.txt"
        file_path.write_text("diagnostic file")
        message, _ = await manager.send_attachment("test-channel", file_path)
    elif kind == "proactive":
        message = await manager.send_proactive("test-channel", "destination", "diagnostic response")
    else:
        message = await manager.send_message("test-channel", "diagnostic response")
    failure_lines = [
        record.getMessage() for record in caplog.records if "Failed to send" in record.getMessage()
    ]
    assert len(failure_lines) == 1
    assert message.id in failure_lines[0]
    assert "ReadError" in failure_lines[0]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["attachment", "proactive"])
async def test_stop_drains_other_outbound_sends(tmp_path: Path, kind: str) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    entered = asyncio.Event()
    released = asyncio.Event()
    closed = False

    async def send(*args: object) -> str:
        entered.set()
        await released.wait()
        if closed:
            raise httpx.ReadError("")
        return "other-delivered"

    async def close() -> None:
        nonlocal closed
        closed = True

    adapter.send_attachment = AsyncMock(side_effect=send)
    adapter.send_proactive = AsyncMock(side_effect=send)
    adapter.shutdown.side_effect = close
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()

    async def deliver() -> CommsMessage:
        if kind == "attachment":
            file_path = tmp_path / "response.txt"
            file_path.write_text("response file")
            message, _ = await manager.send_attachment("test-channel", file_path)
            return message
        return await manager.send_proactive("test-channel", "destination", "response")

    sending = asyncio.create_task(deliver())
    await entered.wait()
    with patch.object(manager.responder, "stop", AsyncMock(side_effect=released.set)):
        await manager.stop()
    message = await sending
    assert message.status == "sent"
    assert message.platform_message_id == "other-delivered"
    assert closed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_caller_send_is_still_drained() -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    entered = asyncio.Event()
    released = asyncio.Event()
    delivered = asyncio.Event()

    async def send(message: CommsMessage) -> str:
        entered.set()
        await released.wait()
        delivered.set()
        return "cancelled-caller-delivery"

    adapter.send_message.side_effect = send
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()
    sending = asyncio.create_task(manager.send_message("test-channel", "caller response"))
    await entered.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    with patch.object(manager.responder, "stop", AsyncMock(side_effect=released.set)):
        await manager.stop()
    assert delivered.is_set()
    assert store.update_message_delivery.call_args.args[1] == "sent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_bounds_a_send_that_ignores_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    entered = asyncio.Event()
    released = asyncio.Event()
    cancelled = asyncio.Event()
    messages: list[CommsMessage] = []

    async def send(message: CommsMessage) -> str:
        messages.append(message)
        entered.set()
        try:
            await released.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await released.wait()
        return "late-delivery"

    adapter.send_message.side_effect = send
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()
    sending = asyncio.create_task(manager.send_message("test-channel", "stubborn response"))
    await entered.wait()
    with (
        patch("gobby.communications.outbound._OUTBOUND_DRAIN_SECONDS", 0),
        patch("gobby.communications.outbound._OUTBOUND_CANCEL_SECONDS", 0, create=True),
    ):
        stopping = asyncio.create_task(manager.stop())
        try:
            await cancelled.wait()
            done, _ = await asyncio.wait({stopping}, timeout=0.5)
            assert stopping in done
            store.create_message.assert_called_once()
            assert messages[0].id in caplog.text
            assert "TimeoutError" in caplog.text
        finally:
            released.set()
            await stopping
            await sending


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_manager_stop_settles_outbound_before_propagating() -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    adapter = make_adapter()
    entered = asyncio.Event()
    responder_stopping = asyncio.Event()
    never = asyncio.Event()

    async def send(message: CommsMessage) -> str:
        entered.set()
        await never.wait()
        return "unreachable"

    async def stop_responder() -> None:
        responder_stopping.set()
        await never.wait()

    adapter.send_message.side_effect = send
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager.start()
    sending = asyncio.create_task(manager.send_message("test-channel", "outer deadline"))
    await entered.wait()
    with patch.object(manager.responder, "stop", AsyncMock(side_effect=stop_responder)):
        stopping = asyncio.create_task(manager.stop())
        await responder_stopping.wait()
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
    with pytest.raises(asyncio.CancelledError):
        await sending
    store.update_message_delivery.assert_called_once()
    assert store.update_message_delivery.call_args.args[1] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_reaction_delegates_to_capable_adapter() -> None:
    channel = make_channel(channel_type="telegram")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    mock_adapter = make_adapter(channel_type="telegram")
    mock_adapter.supports_reactions = True
    mock_adapter.set_reaction = AsyncMock()

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    assert manager.supports_reactions("test-channel") is True
    await manager.set_reaction("test-channel", "chat-1", "message-1", "👀")

    mock_adapter.set_reaction.assert_awaited_once_with(
        "chat-1",
        "message-1",
        "👀",
    )
    manager._adapters.clear()
    assert manager.supports_reactions("test-channel") is False
    with pytest.raises(ValueError, match="not found or not active"):
        await manager.set_reaction("test-channel", "chat-1", "message-1", None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_edit_message_persists_final_content(temp_db: HubDatabase) -> None:
    channel = make_channel(channel_id="00000000-0000-0000-0000-000000000101")
    store = LocalCommunicationsStore(
        temp_db,
        project_id="00000000-0000-0000-0000-000000000000",
    )
    store.create_channel(channel)
    stored_message = CommsMessage(
        id="00000000-0000-0000-0000-000000000102",
        channel_id=channel.id,
        direction="outbound",
        content="Thinking…",
        platform_message_id="platform-msg-id-1",
        created_at=datetime.now(UTC),
    )
    store.create_message(stored_message)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.supports_message_edit = True
    mock_adapter.edit_message = AsyncMock()
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    await manager.edit_message(
        "test-channel",
        "platform-msg-id-1",
        "Final response",
        "conversation-1",
    )

    mock_adapter.edit_message.assert_awaited_once_with(
        "platform-msg-id-1",
        "Final response",
        "conversation-1",
    )
    persisted = store.get_message("00000000-0000-0000-0000-000000000102")
    assert persisted is not None
    assert persisted.content == "Final response"
    assert [message.id for message in store.list_messages(channel_id=channel.id)] == [
        "00000000-0000-0000-0000-000000000102"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_out_of_range_callback_ttl_rejects_before_send(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An out-of-range callback TTL is caller input: no send, stored row or error log."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    mock_adapter = make_adapter()
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()
    metadata = {
        "inline_keyboard": [[{"text": "A", "value": "a"}]],
        "callback_ttl_seconds": 86400,
    }

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(ValueError, match=r"between 1 and 3600 \(got 86400\)"),
    ):
        await manager.send_message("test-channel", "Pick one", metadata=metadata)

    mock_adapter.send_message.assert_not_awaited()
    store.create_message.assert_not_called()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("send", ["message", "attachment"])
async def test_send_rejects_a_session_id_without_an_exact_session_row(
    send: str,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A sender can attach only an existing session: no prefix match, no send, no row (#23292)."""
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="outbound-session-guard",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Lane 8",
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_id="44444444-4444-4444-8444-444444444444")
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    mock_adapter = make_adapter()
    mock_adapter.send_attachment = AsyncMock()
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()
    attachment = tmp_path / "note.txt"
    attachment.write_text("note")

    async def send_with(session_id: str) -> None:
        if send == "message":
            await manager.send_message("test-channel", "Hello", session_id=session_id)
        else:
            await manager.send_attachment("test-channel", attachment, session_id=session_id)

    for unknown in ("55555555-5555-4555-8555-555555555555", session.id[:8], ""):
        with pytest.raises(ValueError, match="Unknown session"):
            await send_with(unknown)

    mock_adapter.send_message.assert_not_awaited()
    mock_adapter.send_attachment.assert_not_awaited()
    assert store.list_messages(channel_id=channel.id) == []
    assert store.session_exists(session.id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_max_callback_ttl_sends_buttons() -> None:
    """The largest allowed callback TTL reaches the adapter with the keyboard intact."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    mock_adapter = make_adapter()
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()
    keyboard = [[{"text": "A", "value": "a"}]]

    msg = await manager.send_message(
        "test-channel",
        "Pick one",
        metadata={"inline_keyboard": keyboard, "callback_ttl_seconds": 3600},
    )

    assert msg.status == "sent"
    sent = mock_adapter.send_message.await_args.args[0]
    assert sent.metadata_json["inline_keyboard"] == keyboard
    assert sent.metadata_json["callback_ttl_seconds"] == 3600
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_unknown_channel_raises() -> None:
    """send_message() raises ValueError for unknown channel."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with pytest.raises(ValueError, match="not found or not active"):
        await manager.send_message("no-such-channel", "Hello!")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("attachment", [False, True])
async def test_outbound_send_waits_for_enabled_channel_initialization(
    tmp_path: Path, attachment: bool
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    initialized = asyncio.Event()
    release = asyncio.Event()
    adapter = MagicMock()
    adapter.send_message = AsyncMock(return_value="platform-1")
    adapter.send_attachment = AsyncMock(return_value="platform-2")

    async def start_adapter() -> None:
        initialized.set()
        await release.wait()
        manager._channel_by_name[channel.name] = channel
        manager._adapters[channel.name] = adapter

    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")

    async def send() -> CommsMessage:
        if attachment:
            message, _ = await manager.send_attachment(channel.name, file_path)
            return message
        return await manager.send_message(channel.name, "Hello!")

    with (
        patch.object(manager._lifecycle, "start", side_effect=start_adapter),
        patch.object(manager._outbound, "enrich_metadata", return_value={}),
    ):
        startup = asyncio.create_task(manager.start())
        await initialized.wait()
        outbound = asyncio.create_task(send())
        await asyncio.sleep(0)
        assert not outbound.done()

        release.set()
        await startup
        message = await outbound
    assert message.status == "sent"
    if attachment:
        adapter.send_attachment.assert_awaited_once()
    else:
        adapter.send_message.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_adapter_failure_marks_failed() -> None:
    """send_message() marks message failed if adapter raises."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.send_message = AsyncMock(side_effect=RuntimeError("network error"))
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert msg.status == "failed"
    assert "network error" in (msg.error or "")
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_failure_with_empty_exception_text_names_the_type() -> None:
    """An exception with empty str() (httpx.ConnectError) still leaves a usable error."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.send_message = AsyncMock(side_effect=httpx.ConnectError(""))
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    stored_message = store.create_message.call_args.args[0]
    assert msg.status == "failed"
    assert msg.error == "ConnectError"
    assert stored_message.error == "ConnectError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_rate_limit_timeout_marks_failed() -> None:
    """send_message() marks message failed if rate-limit waiting exceeds its bound."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with patch.object(
        manager._rate_limiter,
        "wait_if_needed",
        AsyncMock(side_effect=RateLimitWaitExceeded("rate limit wait exceeded")),
    ):
        msg = await manager.send_message("test-channel", "Hello!")

    assert msg.status == "failed"
    assert "rate limit wait exceeded" in (msg.error or "")
    mock_adapter.send_message.assert_not_called()
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_proactive_rate_limits_and_persists_message() -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with patch.object(manager._rate_limiter, "wait_if_needed", AsyncMock()) as wait_mock:
        msg = await manager.send_proactive("test-channel", "conversation-1", "Hello!")

    wait_mock.assert_awaited_once_with("chan-1")
    mock_adapter.send_proactive.assert_awaited_once_with("conversation-1", "Hello!", "text")
    assert msg.status == "sent"
    assert msg.platform_message_id == "platform-proactive-id-1"
    assert msg.metadata_json["platform_destination"] == "conversation-1"
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_gobby_chat_without_broadcast_marks_failed() -> None:
    channel = make_channel(
        name="gobby_chat",
        channel_type="gobby_chat",
        channel_id="gobby-chat-1",
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    await manager.start()

    msg = await manager.send_message("gobby_chat", "Hello!")

    assert msg.status == "failed"
    assert "broadcast callable is not configured" in (msg.error or "")
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_set_websocket_broadcast_before_start_wires_gobby_chat_on_start() -> None:
    channel = make_channel(
        name="gobby_chat",
        channel_type="gobby_chat",
        channel_id="gobby-chat-1",
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    broadcast = AsyncMock()

    manager.set_websocket_broadcast(broadcast)
    await manager.start()

    msg = await manager.send_message("gobby_chat", "Hello!")

    assert msg.status == "sent"
    assert msg.platform_message_id is not None
    broadcast.assert_awaited_once()
    assert broadcast.await_args is not None
    payload = broadcast.await_args.args[0]
    assert payload["content"] == "Hello!"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_telegram_4xx_redacts_token_from_logs_and_storage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "test-telegram-token"
    channel = make_channel(
        channel_type="telegram",
        config_json={
            "bot_token": "$secret:TELEGRAM_BOT_TOKEN",
            "default_destination": "chat999",
        },
    )
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = token
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_post = AsyncMock()

    async def side_effect(url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("POST", url)
        if "deleteWebhook" in url:
            return httpx.Response(200, request=request, json={"ok": True})
        if "getMe" in url:
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"id": 123456, "username": "gobby_bot"}},
            )
        if "setMyCommands" in url:
            return httpx.Response(200, request=request, json={"ok": True, "result": True})
        return httpx.Response(400, request=request, json={"ok": False})

    mock_post.side_effect = side_effect

    with (
        patch("httpx.AsyncClient") as MockClient,
        patch("gobby.communications.manager.get_adapter_class", return_value=TelegramAdapter),
    ):
        MockClient.return_value.post = mock_post
        await manager.start()

        with caplog.at_level(logging.ERROR, logger="gobby.communications.manager"):
            msg = await manager.send_message("test-channel", "Hello!")

    stored_message = store.create_message.call_args.args[0]
    assert msg.status == "failed"
    assert msg.error == stored_message.error
    assert stored_message.error is not None
    assert "400" in stored_message.error
    assert "***" in stored_message.error
    assert token not in stored_message.error
    assert token not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_fires_event_callback() -> None:
    """send_message() fires event_callback after send."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    callback_events = []

    async def cb(event_type: str, **kwargs: Any) -> None:
        callback_events.append((event_type, kwargs))

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager.event_callback = cb
    await manager.send_message("test-channel", "Hello!")

    assert len(callback_events) == 1
    assert callback_events[0][0] == "comms.message_sent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_logs_event_callback_failures_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    async def cb(event_type: str, **kwargs: Any) -> None:
        raise RuntimeError("callback broke")

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager.event_callback = cb
    with caplog.at_level(logging.WARNING):
        await manager.send_message("test-channel", "Hello!")

    assert "Event callback error on send_message" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_stores_messages() -> None:
    """handle_inbound() parses and stores messages."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    parsed_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hi there!",
        created_at=_FIXED_TS,
    )

    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel", {"data": "payload"}, {}, raw_body=b'{"data":"payload"}'
    )

    assert len(stored) == 1
    assert stored[0].content == "Hi there!"
    store.create_message.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_persists_downloaded_attachments() -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    parsed_msg = CommsMessage(
        id="msg-1",
        channel_id="",
        direction="inbound",
        content="document caption",
        content_type="attachment",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="attachment-1",
        message_id="",
        filename="report.pdf",
        content_type="application/pdf",
        size_bytes=17,
        local_path="/tmp/report.pdf",
    )
    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter.download_inbound_attachments.return_value = [attachment]

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel",
        {"data": "payload"},
        {},
        raw_body=b'{"data":"payload"}',
    )

    assert len(stored) == 1
    mock_adapter.download_inbound_attachments.assert_awaited_once_with(
        stored[0],
        manager.attachment_manager,
    )
    assert attachment.message_id == stored[0].id
    store.create_message_with_attachments.assert_called_once_with(stored[0], [attachment])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_transcribes_voice_note_before_event(tmp_path: Path) -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"telegram voice bytes")
    parsed_msg = CommsMessage(
        id="voice-message",
        channel_id="",
        direction="inbound",
        content="original caption",
        content_type="attachment",
        metadata_json={"voice_note": True},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="voice-attachment",
        message_id="",
        filename="voice.ogg",
        content_type="audio/ogg",
        size_bytes=20,
        local_path=str(audio_path),
    )
    transcriber = MagicMock()
    transcriber.transcribe = AsyncMock(return_value="  transcribed voice note  ")
    manager.set_voice_transcriber_getter(lambda: transcriber)
    manager.event_callback = AsyncMock()
    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter.download_inbound_attachments.return_value = [attachment]

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel",
        {"data": "payload"},
        {},
        raw_body=b'{"data":"payload"}',
    )

    assert stored[0].content == "transcribed voice note"
    assert stored[0].metadata_json["voice_note_caption"] == "original caption"
    assert stored[0].metadata_json["voice_transcription_status"] == "completed"
    assert attachment.message_id == stored[0].id
    transcriber.transcribe.assert_awaited_once_with(b"telegram voice bytes", "audio/ogg")
    store.create_message_with_attachments.assert_called_once_with(stored[0], [attachment])
    manager.event_callback.assert_awaited_once_with(
        "comms.message_received",
        message=stored[0],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_voice_transcription_timeout_preserves_message_and_marks_failed(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"voice bytes")
    message = CommsMessage(
        id="voice-message",
        channel_id="channel-1",
        direction="inbound",
        content="original caption",
        content_type="attachment",
        metadata_json={"voice_note": True},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="voice-attachment",
        message_id="voice-message",
        filename="voice.ogg",
        content_type="audio/ogg",
        size_bytes=11,
        local_path=str(audio_path),
    )
    transcriber = MagicMock()

    async def slow_transcribe(*_args: object) -> str:
        await asyncio.Event().wait()
        return "late transcript"

    transcriber.transcribe = AsyncMock(side_effect=slow_transcribe)

    await apply_voice_transcription(
        message,
        [attachment],
        transcriber,
        timeout_seconds=0.001,
    )

    assert message.content == "original caption"
    assert message.metadata_json["voice_transcription_status"] == "failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_describes_sticker_before_event(tmp_path: Path) -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    image_path = tmp_path / "sticker.webp"
    image_path.write_bytes(b"telegram sticker image")
    parsed_msg = CommsMessage(
        id="sticker-message",
        channel_id="",
        direction="inbound",
        content="",
        content_type="attachment",
        metadata_json={
            "telegram_sticker": {
                "format": "static",
                "emoji": "🦡",
                "set_name": "quartz_badger",
                "type": "regular",
            }
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    attachment = CommsAttachment(
        id="sticker-attachment",
        message_id="",
        filename="sticker.webp",
        content_type="image/webp",
        size_bytes=22,
        local_path=str(image_path),
    )
    vision_result = MagicMock(
        text="A badger holding a sparkling quartz crystal.",
        provider="claude",
        model="sonnet",
    )
    vision_service = MagicMock()
    vision_service.extract = AsyncMock(return_value=vision_result)
    manager.set_vision_extract_service(vision_service)
    manager.event_callback = AsyncMock()
    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter.download_inbound_attachments.return_value = [attachment]

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    stored = await manager.handle_inbound(
        "test-channel",
        {"data": "payload"},
        {},
        raw_body=b'{"data":"payload"}',
    )

    assert stored[0].content == (
        "Telegram sticker 🦡: A badger holding a sparkling quartz crystal."
    )
    assert stored[0].metadata_json["sticker_vision_status"] == "completed"
    request = vision_service.extract.await_args.args[0]
    assert request.image_path == str(image_path)
    assert attachment.message_id == stored[0].id
    store.create_message_with_attachments.assert_called_once_with(stored[0], [attachment])
    manager.event_callback.assert_awaited_once_with(
        "comms.message_received",
        message=stored[0],
    )


@pytest.mark.unit
async def test_handle_inbound_webhook_verification_failure() -> None:
    """handle_inbound() raises ValueError if webhook signature fails."""
    channel = make_channel(webhook_secret="mysecret")
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = "mysecret"
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.verify_webhook.return_value = False
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    with pytest.raises(ValueError, match="signature verification failed"):
        await manager.handle_inbound("test-channel", b"payload", {"X-Signature": "bad"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_verifies_adapter_when_webhook_secret_unset() -> None:
    """Adapters still reject forged webhooks when channel.webhook_secret is unset."""
    manager = CommunicationsManager(make_config(), make_store([]), make_secret_store(), MagicMock())
    slack_adapter = SlackAdapter()
    sms_adapter = SMSAdapter()
    teams_adapter = TeamsAdapter()
    slack_adapter._signing_secret = "slack-signing-secret"
    sms_adapter._auth_token = "twilio-auth-token"
    sms_adapter._webhook_url = "https://example.com/hooks/sms"
    teams_adapter._app_id = "teams-app-id"
    cases = [
        ("slack-channel", make_channel("slack-channel", "slack"), slack_adapter, {}),
        (
            "sms-channel",
            make_channel("sms-channel", "sms"),
            sms_adapter,
            {"x-twilio-signature": "bad"},
        ),
        ("teams-channel", make_channel("teams-channel", "teams"), teams_adapter, {}),
    ]

    for channel_name, channel, adapter, headers in cases:
        manager._adapters[channel_name] = adapter
        manager._channel_by_name[channel_name] = channel

        with pytest.raises(ValueError, match="signature verification failed"):
            await manager.handle_inbound(channel_name, b"forged", headers)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_resolves_webhook_secret_ref() -> None:
    """handle_inbound() resolves webhook_secret refs before signature verification."""
    channel = make_channel(webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK")
    store = make_store([channel])
    secret_store = make_secret_store()
    secret_store.get.return_value = "mysecret"
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.verify_webhook.side_effect = (
        lambda _payload, _headers, secret: secret == "mysecret"
    )
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    secret_store.get.reset_mock()
    messages = await manager.handle_inbound("test-channel", b"payload", {"X-Signature": "ok"})

    assert messages == []
    secret_store.get.assert_called_once_with("COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK")
    mock_adapter.verify_webhook.assert_called_once_with(
        b"payload",
        {"X-Signature": "ok"},
        "mysecret",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_resolves_identity() -> None:
    """handle_inbound() resolves identity and sets session_id."""
    channel = make_channel()
    store = make_store([channel])

    identity = CommsIdentity(
        id="identity-1",
        channel_id="chan-1",
        external_user_id="ext-user-1",
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
        session_id="session-abc",
    )
    store.get_identity_by_external.return_value = identity

    parsed_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hi!",
        identity_id="ext-user-1",
        created_at=_FIXED_TS,
    )

    mock_adapter = make_adapter()
    mock_adapter.parse_webhook.return_value = [parsed_msg]
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    stored = await manager.handle_inbound("test-channel", {}, {}, raw_body=b"{}")
    assert stored[0].session_id == "session-abc"
    assert stored[0].identity_id == "identity-1"
    assert stored[0].metadata_json["external_user_id"] == "ext-user-1"


@pytest.mark.unit
async def test_handle_inbound_messages_continues_after_identity_resolution_failure() -> None:
    """A bad inbound message should not abort the rest of the batch."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel

    identity = CommsIdentity(
        id="identity-2",
        channel_id="chan-1",
        external_user_id="ext-user-2",
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
        session_id="session-ok",
    )
    object.__setattr__(
        manager._identity_manager,
        "resolve_inbound_identity",
        MagicMock(
            side_effect=[
                RuntimeError("database unavailable"),
                IdentityResolution(identity=identity, session_id="session-ok"),
            ]
        ),
    )

    messages = [
        CommsMessage(
            id="bad-msg",
            channel_id="chan-1",
            direction="inbound",
            content="bad",
            identity_id="ext-user-1",
            created_at=_FIXED_TS,
        ),
        CommsMessage(
            id="good-msg",
            channel_id="chan-1",
            direction="inbound",
            content="good",
            identity_id="ext-user-2",
            created_at=_FIXED_TS,
        ),
    ]

    stored = await manager.handle_inbound_messages("test-channel", messages)

    assert [message.content for message in stored] == ["good"]
    assert stored[0].session_id == "session-ok"
    store.create_message.assert_called_once_with(messages[1])


@pytest.mark.parametrize(
    ("adapter_name", "raw_channel_id", "metadata_json", "expected_platform_channel_id"),
    [
        ("slack", "C123", {}, "C123"),
        ("sms", "+15551234567", {}, "+15551234567"),
        ("teams", "conv-123", {}, "conv-123"),
        ("discord", "discord-channel-123", {}, "discord-channel-123"),
        ("email", "sender@example.com", {}, "sender@example.com"),
        ("telegram", "", {"chat_id": "2222222"}, "2222222"),
        ("gobby_chat", "gobby_chat", {}, "gobby_chat"),
    ],
)
@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_messages_stores_internal_channel_id(
    adapter_name: str,
    raw_channel_id: str,
    metadata_json: dict[str, str],
    expected_platform_channel_id: str,
) -> None:
    """Inbound messages store the internal channel UUID and preserve platform channel."""
    channel = make_channel(channel_id="internal-channel-id")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type=adapter_name)
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    message = CommsMessage(
        id=f"{adapter_name}-msg-1",
        channel_id=raw_channel_id,
        direction="inbound",
        content="Hi",
        metadata_json=metadata_json,
        created_at=_FIXED_TS,
    )

    stored = await manager.handle_inbound_messages("test-channel", [message])

    assert stored[0].channel_id == "internal-channel-id"
    assert stored[0].metadata_json["platform_channel_id"] == expected_platform_channel_id
    store.create_message.assert_called_once_with(stored[0])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adapter_rate_limit_callback_wires_to_limiter() -> None:
    """Verify adapter rate_limit_callback updates the manager's rate limiter."""
    channel = make_channel(channel_id="chan-rate-limit")
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    # Capture the callback that manager sets on the adapter
    captured_callback: Callable[[float, bool], None] | None = None

    def set_callback(cb: Callable[[float, bool], None]) -> None:
        nonlocal captured_callback
        captured_callback = cb

    mock_adapter.set_rate_limit_callback.side_effect = set_callback
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    assert captured_callback is not None

    # Manually trigger the callback
    captured_callback(5.0, False)  # 5 seconds backoff

    # Verify backoff is set in the rate limiter
    # TokenBucketRateLimiter.check should return False due to backoff
    assert manager._rate_limiter.check("chan-rate-limit") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_creates_and_initializes() -> None:
    """add_channel() saves to DB and initializes adapter."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        channel = await manager.add_channel("slack", "my-slack", {"token": "$secret:SLACK_TOKEN"})

    assert channel.name == "my-slack"
    assert channel.channel_type == "slack"
    store.create_channel.assert_called_once()
    assert "my-slack" in manager._adapters
    assert manager.channel_to_dict(channel)["active"] is True
    assert manager.channel_to_dict(channel)["init_error"] is None


@pytest.mark.unit
async def test_add_channel_returns_inactive_with_init_error_on_adapter_failure() -> None:
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter.initialize.side_effect = RuntimeError("bad token")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        channel = await manager.add_channel("slack", "my-slack", {"token": "$secret:SLACK_TOKEN"})

    payload = manager.channel_to_dict(channel)
    assert payload["active"] is False
    assert payload["init_error"] == "bad token"
    assert "my-slack" not in manager._adapters
    store.create_channel.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_stores_secrets_in_secret_store() -> None:
    """add_channel() stores secrets in SecretStore and puts refs in channel config."""
    store = make_store()
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    secrets = {
        "bot_token": "xoxb-test-token",
        "signing_secret": "abc123",
        "webhook_secret": "whsec_keep_separate",
    }

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        channel = await manager.add_channel("slack", "my-slack", {}, secrets=secrets)

    assert channel.webhook_secret == "$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK"

    # bot_token, signing_secret, and webhook_secret stored in SecretStore
    assert secret_store.set.call_count == 3
    set_calls = {call.kwargs["name"]: call for call in secret_store.set.call_args_list}
    assert "COMMS_SLACK_BOT_TOKEN_MY_SLACK" in set_calls
    assert "COMMS_SLACK_SIGNING_SECRET_MY_SLACK" in set_calls
    assert "COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK" in set_calls

    # Config should have $secret: references
    created_channel = store.create_channel.call_args[0][0]
    assert created_channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK"
    assert (
        created_channel.config_json["signing_secret"]
        == "$secret:COMMS_SLACK_SIGNING_SECRET_MY_SLACK"
    )
    assert "webhook_secret" not in created_channel.config_json
    assert created_channel.webhook_secret == "$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK"
    assert secret_store.set.call_args_list[-1].kwargs["plaintext_value"] == "whsec_keep_separate"


@pytest.mark.integration
async def test_add_channel_persists_webhook_secret_reference(
    temp_db: HubDatabase, mock_machine_id: str
) -> None:
    """New channel rows contain a SecretStore reference instead of plaintext."""
    assert mock_machine_id
    store = LocalCommunicationsStore(temp_db)
    secret_store = SecretStore(temp_db)
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())
    mock_adapter = make_adapter(channel_type="slack")

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        channel = await manager.add_channel(
            "slack",
            "db-backed-slack",
            {},
            secrets={"webhook_secret": "plaintext-webhook-secret"},
        )

    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.webhook_secret == "$secret:COMMS_SLACK_WEBHOOK_SECRET_DB_BACKED_SLACK"
    assert (
        secret_store.get("COMMS_SLACK_WEBHOOK_SECRET_DB_BACKED_SLACK") == "plaintext-webhook-secret"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_does_not_mutate_caller_config() -> None:
    store = make_store()
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    config = {"token": "$secret:SLACK_TOKEN"}
    secrets = {"bot_token": "xoxb-test-token"}

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.add_channel("slack", "my-slack", config, secrets=secrets)

    assert config == {"token": "$secret:SLACK_TOKEN"}
    created_channel = store.create_channel.call_args[0][0]
    assert created_channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK"


@pytest.mark.unit
def test_channel_to_dict_redacts_webhook_secret() -> None:
    channel = make_channel(webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MY_SLACK")
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), MagicMock()
    )

    payload = manager.channel_to_dict(channel)

    assert "webhook_secret" not in payload
    assert payload["active"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_init_adapter_offloads_secret_ref_store_reads() -> None:
    loop_thread_id = threading.get_ident()
    secret_store = make_secret_store()

    def get_secret(name: str) -> str | None:
        assert threading.get_ident() != loop_thread_id
        return "resolved-token" if name == "COMMS_TEST_TOKEN" else None

    secret_store.get.side_effect = get_secret
    manager = CommunicationsManager(make_config(), make_store(), secret_store, MagicMock())
    channel = make_channel(config_json={"bot_token": "$secret:COMMS_TEST_TOKEN"})
    adapter = make_adapter()

    async def initialize(
        _config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        assert secret_resolver("$secret:COMMS_TEST_TOKEN") == "resolved-token"
        assert secret_resolver("COMMS_TEST_TOKEN") == "resolved-token"

    adapter.initialize.side_effect = initialize
    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=adapter),
    ):
        await manager._init_adapter(channel)

    secret_store.get.assert_any_call("COMMS_TEST_TOKEN")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_init_adapter_resolves_secret_refs_with_real_secret_store(
    temp_db: HubDatabase, mock_machine_id: str
) -> None:
    """_init_adapter() passes a ref-aware resolver backed by SecretStore.get."""
    assert mock_machine_id
    secret_store = SecretStore(temp_db)
    secret_store.set(
        name="COMMS_SLACK_BOT_TOKEN_MY_SLACK",
        plaintext_value="xoxb-scoped-token",
        category="integration",
    )
    secret_store.set(
        name="COMMS_SLACK_SIGNING_SECRET_MY_SLACK",
        plaintext_value="scoped-signing-secret",
        category="integration",
    )
    manager = CommunicationsManager(make_config(), make_store(), secret_store, MagicMock())
    channel = ChannelConfig(
        id="secret-backed-slack",
        channel_type="slack",
        name="my-slack",
        enabled=True,
        config_json={
            "bot_token": "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK",
            "signing_secret": "$secret:COMMS_SLACK_SIGNING_SECRET_MY_SLACK",
        },
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "user_id": "U12345"}
        mock_post.return_value = mock_response

        adapter = await manager._init_adapter(channel)

    assert isinstance(adapter, SlackAdapter)
    assert adapter._bot_token == "xoxb-scoped-token"
    assert adapter._signing_secret == "scoped-signing-secret"
    assert adapter._bot_user_id == "U12345"
    await adapter.shutdown()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_channel_skips_empty_secrets() -> None:
    """add_channel() skips empty secret values."""
    store = make_store()
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    mock_adapter = make_adapter(channel_type="slack")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    secrets = {"bot_token": "xoxb-real", "signing_secret": "", "webhook_secret": ""}

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.add_channel("slack", "my-slack", {}, secrets=secrets)

    # Only bot_token stored (signing_secret and webhook_secret are empty)
    assert secret_store.set.call_count == 1
    assert secret_store.set.call_args.kwargs["name"] == "COMMS_SLACK_BOT_TOKEN_MY_SLACK"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remove_channel_shuts_down_and_deletes() -> None:
    """remove_channel() shuts down adapter and deletes from DB."""
    channel = make_channel()
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    await manager.remove_channel("test-channel")

    mock_adapter.shutdown.assert_called_once()
    store.delete_channel.assert_called_once_with("chan-1")
    assert "test-channel" not in manager._adapters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remove_channel_not_found_noop() -> None:
    """remove_channel() reports not found only when no DB row exists."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with pytest.raises(ValueError, match="not found"):
        await manager.remove_channel("nonexistent")

    store.delete_channel.assert_not_called()


@pytest.mark.unit
async def test_remove_channel_deletes_inactive_db_row_by_name() -> None:
    channel = make_channel(enabled=False)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    await manager.remove_channel("test-channel")

    store.get_channel_by_name.assert_called_once_with("test-channel")
    store.delete_channel.assert_called_once_with("chan-1")
    assert "test-channel" not in manager._adapters
    assert manager.get_channel_status("test-channel")["status"] == "not_found"


@pytest.mark.unit
def test_list_channels() -> None:
    """list_channels() returns all channels from DB."""
    channels = [make_channel("ch1"), make_channel("ch2", channel_id="chan-2")]
    store = make_store(channels)
    store.list_channels.return_value = channels
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.list_channels()
    assert len(result) == 2
    store.list_channels.assert_called_with(enabled_only=False)


@pytest.mark.unit
def test_get_channel_status_active() -> None:
    """get_channel_status() returns active status for running adapter."""
    channel = make_channel()
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    manager._adapters["test-channel"] = mock_adapter
    manager._channel_by_name["test-channel"] = channel

    status = manager.get_channel_status("test-channel")
    assert status["status"] == "active"
    assert status["active"] is True
    assert status["supports_webhooks"] is True


@pytest.mark.unit
def test_get_channel_status_inactive() -> None:
    """get_channel_status() returns inactive for DB-only channel."""
    channel = make_channel()
    store = make_store()
    store.list_channels.return_value = [channel]
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    status = manager.get_channel_status("test-channel")
    assert status["status"] == "inactive"
    assert status["active"] is False


@pytest.mark.unit
def test_get_channel_status_not_found() -> None:
    """get_channel_status() returns not_found for unknown channel."""
    store = make_store()
    store.list_channels.return_value = []
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    status = manager.get_channel_status("ghost-channel")
    assert status["status"] == "not_found"
    assert status["active"] is False


@pytest.mark.unit
def test_get_channel_delegates_to_store() -> None:
    """get_channel() delegates to store.get_channel()."""
    channel = make_channel()
    store = make_store()
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.get_channel("chan-1")
    assert result == channel
    store.get_channel.assert_called_once_with("chan-1")


@pytest.mark.unit
def test_get_channel_returns_none_for_missing() -> None:
    """get_channel() returns None when channel doesn't exist."""
    store = make_store()
    store.get_channel.return_value = None
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    result = manager.get_channel("nonexistent")
    assert result is None


@pytest.mark.unit
async def test_update_channel_delegates_to_store() -> None:
    """update_channel() delegates to store and sets updated_at."""
    channel = make_channel()
    store = make_store()
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        result = await manager.update_channel(channel)

    assert result == channel
    store.update_channel.assert_called_once_with(channel)
    # updated_at should be refreshed
    assert channel.updated_at != _FIXED_TS


@pytest.mark.unit
async def test_update_channel_stores_changed_secrets() -> None:
    channel = make_channel(channel_type="slack", name="my-slack")
    store = make_store()
    store.update_channel.return_value = channel
    secret_store = make_secret_store()
    manager = CommunicationsManager(make_config(), store, secret_store, MagicMock())

    with patch("gobby.communications.manager.get_adapter_class", return_value=None):
        result = await manager.update_channel(channel, secrets={"bot_token": "new-token"})

    assert result == channel
    secret_store.set.assert_called_once_with(
        name="COMMS_SLACK_BOT_TOKEN_MY_SLACK",
        plaintext_value="new-token",
        category="integration",
        description="slack channel 'my-slack': bot_token",
    )
    assert channel.config_json["bot_token"] == "$secret:COMMS_SLACK_BOT_TOKEN_MY_SLACK"


@pytest.mark.unit
async def test_update_channel_disable_stops_runtime_traffic() -> None:
    channel = make_channel()
    store = make_store([channel])
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._polling_manager = MagicMock()
    adapter = make_adapter(supports_polling=True)
    manager._adapters[channel.name] = adapter
    manager._channel_by_name[channel.name] = channel

    channel.enabled = False

    result = await manager.update_channel(channel)

    assert result == channel
    manager._polling_manager.stop_polling.assert_called_once_with(channel.name)
    adapter.shutdown.assert_awaited_once()
    assert channel.name not in manager._adapters
    assert channel.name not in manager._channel_by_name
    assert manager.channel_to_dict(channel)["active"] is False


@pytest.mark.unit
async def test_update_channel_enabled_reinitializes_and_refreshes_runtime_state() -> None:
    channel = make_channel(config_json={"rate_limit_per_minute": 7, "burst": 3})
    store = make_store([channel])
    store.update_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._rate_limiter = MagicMock()
    old_adapter = make_adapter()
    new_adapter = make_adapter()
    manager._adapters[channel.name] = old_adapter
    manager._channel_by_name[channel.name] = channel
    mock_adapter_cls = MagicMock(return_value=new_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        result = await manager.update_channel(channel)

    assert result == channel
    old_adapter.shutdown.assert_awaited_once()
    new_adapter.initialize.assert_awaited_once()
    assert manager._adapters[channel.name] == new_adapter
    assert manager._channel_by_name[channel.name] == channel
    manager._rate_limiter.configure_channel.assert_called_once_with("chan-1", 7, 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_injects_platform_destination() -> None:
    """send_message() injects platform_destination from channel config."""
    channel = make_channel(config_json={"default_destination": "C0123ABCD"})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert msg.metadata_json.get("platform_destination") == "C0123ABCD"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_preserves_caller_platform_destination() -> None:
    """send_message() does not override platform_destination if caller provided it."""
    channel = make_channel(config_json={"default_destination": "C0123ABCD"})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message(
        "test-channel", "Hello!", metadata={"platform_destination": "COVERRIDE"}
    )

    assert msg.metadata_json["platform_destination"] == "COVERRIDE"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_no_platform_destination_without_config() -> None:
    """send_message() does not inject platform_destination when channel has no default."""
    channel = make_channel(config_json={})
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!")

    assert "platform_destination" not in msg.metadata_json


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_injects_conversation_reference_destination() -> None:
    """send_message() injects Teams conversation reference destination fields."""
    channel = make_channel(channel_type="teams", config_json={})
    identity = CommsIdentity(
        id="identity-1",
        channel_id=channel.id,
        external_user_id="teams-user-1",
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
        session_id="session-abc",
        metadata_json={
            "conversation_reference": {
                "conversation_id": "teams-conversation-1",
                "service_url": "https://smba.trafficmanager.net/apis/",
            }
        },
    )
    store = make_store([channel])
    store.list_identities.return_value = [identity]
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter(channel_type="teams")
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    msg = await manager.send_message("test-channel", "Hello!", session_id="session-abc")

    assert msg.channel_id == channel.id
    assert msg.metadata_json["platform_destination"] == "teams-conversation-1"
    assert msg.metadata_json["service_url"] == "https://smba.trafficmanager.net/apis/"
    assert (
        msg.metadata_json["conversation_reference"]
        == identity.metadata_json["conversation_reference"]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_inbound_session_reply_resolves_chat_destination() -> None:
    """A session auto-created from Telegram inbound can send back to the originating chat."""
    channel = make_channel(
        channel_type="telegram",
        config_json={"allow_from": ["1111111"]},
    )
    identities: list[CommsIdentity] = []
    store = make_store([channel])
    store.get_identity_by_external.side_effect = lambda channel_id, external_user_id: next(
        (
            identity
            for identity in identities
            if identity.channel_id == channel_id and identity.external_user_id == external_user_id
        ),
        None,
    )

    def create_identity(identity: CommsIdentity) -> CommsIdentity:
        identities.append(identity)
        return identity

    store.create_identity.side_effect = create_identity
    store.list_identities.side_effect = lambda channel_id=None: [
        identity
        for identity in identities
        if channel_id is None or identity.channel_id == channel_id
    ]

    session_store = MagicMock()
    session_store.register.return_value = MagicMock(id="telegram-session-1")
    manager = CommunicationsManager(
        make_config(),
        store,
        make_secret_store(),
        session_store,
    )
    mock_adapter = make_adapter(channel_type="telegram")

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    inbound = TelegramAdapter().parse_webhook(
        {
            "update_id": 10000,
            "message": {
                "message_id": 1365,
                "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
                "chat": {"id": 2222222, "type": "private"},
                "date": 1441645532,
                "text": "hello",
            },
        },
        {},
    )
    stored = await manager.handle_inbound_messages("test-channel", inbound)

    assert stored[0].session_id == "telegram-session-1"

    reply = await manager.send_message(
        "test-channel",
        "Hello!",
        session_id="telegram-session-1",
    )

    assert reply.status == "sent"
    sent_message = mock_adapter.send_message.await_args.args[0]
    assert sent_message.metadata_json["platform_destination"] == "2222222"
    assert sent_message.metadata_json["conversation_reference"] == {
        "conversation_id": "2222222",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_message_propagates_thread_id() -> None:
    """send_message() should include platform_thread_id from thread map."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_adapter = make_adapter()
    mock_adapter.send_message.return_value = "out-msg-1"
    mock_adapter_cls = MagicMock(return_value=mock_adapter)

    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    manager._thread_manager.track_thread("chan-1", "session-123", "thread-456")

    msg = await manager.send_message("test-channel", "Hello reply", session_id="session-123")

    assert msg.platform_thread_id == "thread-456"
    assert msg.status == "sent"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected_thread"),
    [
        ({"platform_destination": "42"}, "tracked-thread"),
        ({"platform_destination": "42", "thread_id": None}, None),
        ({"platform_destination": "42", "thread_id": ""}, None),
        ({"platform_destination": "42", "thread_id": "  "}, None),
        ({"platform_destination": "42", "thread_id": " explicit-thread "}, "explicit-thread"),
    ],
)
async def test_send_message_explicit_thread_id_overrides_tracked_thread(
    metadata: dict[str, str | None], expected_thread: str | None
) -> None:
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    mock_adapter = make_adapter()

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    manager._thread_manager.track_thread("chan-1", "session-123", "tracked-thread")
    message = await manager.send_message(
        "test-channel",
        "Explicit reply",
        session_id="session-123",
        metadata=metadata,
    )

    assert message.platform_thread_id == expected_thread
    sent = mock_adapter.send_message.call_args.args[0]
    assert sent.platform_thread_id == expected_thread
    assert sent.metadata_json["platform_destination"] == "42"
    await manager.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_deduplicates_platform_message_and_returns_it_for_ack() -> None:
    channel = make_channel()
    store = make_store([channel])
    store.get_message_by_platform_id.return_value = CommsMessage(
        id="stored-message",
        channel_id=channel.id,
        direction="inbound",
        content="already handled",
        platform_message_id="platform-message-1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    manager._identity_manager = MagicMock()
    manager.event_callback = AsyncMock()

    duplicate = CommsMessage(
        id="duplicate-delivery",
        channel_id="platform-chat-1",
        direction="inbound",
        content="already handled",
        platform_message_id="platform-message-1",
        identity_id="platform-user-1",
        metadata_json={"telegram_update_id": 501},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    handled = await manager.handle_inbound_messages(channel.name, [duplicate])

    assert handled == [store.get_message_by_platform_id.return_value]
    store.get_message_by_platform_id.assert_called_once_with(
        channel.name, duplicate.platform_message_id
    )
    store.create_message.assert_not_called()
    manager._identity_manager.resolve_identity.assert_not_called()
    manager.event_callback.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inbound_deduplicates_reaction_before_action_dispatch() -> None:
    channel = make_channel()
    store = make_store([channel])
    store.get_message_by_platform_id.return_value = CommsMessage(
        id="stored-reaction",
        channel_id=channel.id,
        direction="inbound",
        content="👍",
        content_type="reaction",
        platform_message_id="reaction:501:message-1",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    manager.reaction_handler = AsyncMock()

    duplicate = CommsMessage(
        id="duplicate-reaction",
        channel_id="platform-chat-1",
        direction="inbound",
        content="👍",
        content_type="reaction",
        platform_message_id="reaction:501:message-1",
        identity_id="platform-user-1",
        metadata_json={
            "reaction_target_message_id": "message-1",
            "telegram_update_id": 501,
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    handled = await manager.handle_inbound_messages(channel.name, [duplicate])

    assert handled == [store.get_message_by_platform_id.return_value]
    manager.reaction_handler.handle_reaction.assert_not_awaited()
    store.create_message.assert_not_called()


@pytest.mark.unit
async def test_handle_inbound_populates_thread_map_and_handles_reactions() -> None:
    """handle_inbound_messages() should populate thread map and dispatch reactions."""
    channel = make_channel(webhook_secret=None)
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())

    mock_identity = CommsIdentity(
        id="id-1",
        channel_id="chan-1",
        external_user_id="user-1",
        session_id="session-123",
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
    )

    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_inbound_identity = MagicMock(
        return_value=IdentityResolution(identity=mock_identity, session_id="session-123")
    )

    manager.reaction_handler = AsyncMock()

    inbound_msg = CommsMessage(
        id="msg-1",
        channel_id="chan-1",
        direction="inbound",
        content="Hello",
        platform_thread_id="thread-456",
        created_at=_FIXED_TS,
        identity_id="user-1",
    )

    rxn_msg = CommsMessage(
        id="rxn-1",
        channel_id="chan-1",
        direction="inbound",
        content="+1",
        platform_message_id="reaction:501:msg-123",
        content_type="reaction",
        created_at=_FIXED_TS,
        identity_id="user-1",
        metadata_json={"reaction_target_message_id": "msg-123"},
    )

    # Needs to be dict-like so _channel_by_name works; manager.start() does that.
    mock_adapter = make_adapter()
    mock_adapter_cls = MagicMock(return_value=mock_adapter)
    with patch("gobby.communications.manager.get_adapter_class", return_value=mock_adapter_cls):
        await manager.start()

    handled = await manager.handle_inbound_messages(
        "test-channel",
        [inbound_msg, rxn_msg],
    )

    assert manager._thread_manager._thread_map[("chan-1", "session-123")] == "thread-456"
    assert handled == [inbound_msg, rxn_msg]
    store.create_message.assert_any_call(rxn_msg)

    # reaction should have called handler
    manager.reaction_handler.handle_reaction.assert_awaited_once_with(
        "test-channel", "msg-123", "+1", "user-1"
    )
    assert manager.reaction_handler.handle_reaction.await_count == 1
    assert manager.reaction_handler.handle_reaction.await_args is not None


@pytest.mark.unit
def test_thread_map_lru_eviction_order() -> None:
    """Unit test of internal LRU thread map — no public API exposes this behavior."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._thread_manager._max_size = 3

    # Add 3 entries
    manager._track_thread("ch", "s1", "t1")
    manager._track_thread("ch", "s2", "t2")
    manager._track_thread("ch", "s3", "t3")

    # Access s1 to make it recently used
    assert manager._get_thread_id("ch", "s1") == "t1"

    # Add a 4th entry — should evict s2 (LRU), NOT s1 (recently accessed)
    manager._track_thread("ch", "s4", "t4")

    assert manager._get_thread_id("ch", "s1") == "t1"  # Still present (was accessed)
    assert manager._get_thread_id("ch", "s2") is None  # Evicted (LRU)
    assert manager._get_thread_id("ch", "s3") == "t3"  # Still present
    assert manager._get_thread_id("ch", "s4") == "t4"  # Newly added


@pytest.mark.unit
def test_thread_map_move_to_end_on_track() -> None:
    """Unit test of internal LRU refresh — no public API exposes this behavior."""
    store = make_store()
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._thread_manager._max_size = 2

    manager._track_thread("ch", "s1", "t1")
    manager._track_thread("ch", "s2", "t2")

    # Re-track s1 (refreshes its position)
    manager._track_thread("ch", "s1", "t1-updated")

    # Add s3 — should evict s2 (now LRU), not s1
    manager._track_thread("ch", "s3", "t3")

    assert manager._get_thread_id("ch", "s1") == "t1-updated"
    assert manager._get_thread_id("ch", "s2") is None  # Evicted
    assert manager._get_thread_id("ch", "s3") == "t3"


def _telegram_group_message(*, sender_id: str, mentioned: bool) -> CommsMessage:
    return CommsMessage(
        id=f"group-{sender_id}-{mentioned}",
        channel_id="",
        direction="inbound",
        content="ambient group discussion",
        identity_id=sender_id,
        metadata_json={
            "chat_id": "-100123",
            "platform_channel_id": "-100123",
            "conversation_type": "supergroup",
            "mentioned": mentioned,
            "external_username": sender_id,
        },
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentioned", "expected_passive"),
    [(False, True), (True, False)],
)
@pytest.mark.unit
async def test_configured_group_message_is_classified_before_persistence(
    mentioned: bool,
    expected_passive: bool,
) -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={
            "allow_from": ["owner"],
            "group_policy": "allowlist",
            "groups": {"-100123": {}},
            "require_mention": True,
        },
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    identity = CommsIdentity(
        id="identity-owner",
        channel_id=channel.id,
        external_user_id="owner",
        session_id="group-session",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    object.__setattr__(
        manager._identity_manager,
        "resolve_inbound_identity",
        MagicMock(return_value=IdentityResolution(identity=identity, session_id="group-session")),
    )
    message = _telegram_group_message(sender_id="owner", mentioned=mentioned)

    handled = await manager.handle_inbound_messages(channel.name, [message])

    assert len(handled) == 1
    assert handled[0].metadata_json["passive_context"] is expected_passive
    store.create_message.assert_called_once_with(message)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_group_allowlist_rejection_happens_before_identity_or_persistence() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={
            "allow_from": ["owner"],
            "group_policy": "allowlist",
            "groups": {"-100123": {}},
            "require_mention": True,
        },
    )
    store = make_store([channel])
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    resolve_identity = MagicMock()
    object.__setattr__(
        manager._identity_manager,
        "resolve_inbound_identity",
        resolve_identity,
    )
    message = _telegram_group_message(sender_id="stranger", mentioned=True)

    handled = await manager.handle_inbound_messages(channel.name, [message])

    assert handled == [message]
    resolve_identity.assert_not_called()
    store.create_message.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telegram_reply_targets_originating_session_with_shared_chat() -> None:
    channel = make_channel(
        channel_type="telegram",
        config_json={"allow_from": ["1111111"]},
    )
    store = make_store([channel])
    created_at = datetime(2024, 1, 1, tzinfo=UTC)
    outbound_by_platform_id = {
        "1001": CommsMessage(
            id="outbound-a",
            channel_id=channel.id,
            direction="outbound",
            content="Session A",
            platform_message_id="1001",
            session_id="session-a",
            metadata_json={"platform_destination": "2222222"},
            created_at=created_at,
        ),
        "1002": CommsMessage(
            id="outbound-b",
            channel_id=channel.id,
            direction="outbound",
            content="Session B",
            platform_message_id="1002",
            session_id="session-b",
            metadata_json={"platform_destination": "2222222"},
            created_at=created_at,
        ),
    }

    def get_message_by_platform_id(
        channel_name: str,
        platform_message_id: str,
        *,
        platform_destination: str | None = None,
    ) -> CommsMessage | None:
        if channel_name != channel.name or platform_destination != "2222222":
            return None
        return outbound_by_platform_id.get(platform_message_id)

    store.get_message_by_platform_id.side_effect = get_message_by_platform_id
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    identity = CommsIdentity(
        id="identity-1",
        channel_id=channel.id,
        external_user_id="1111111",
        session_id="session-b",
        created_at=created_at,
        updated_at=created_at,
    )
    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_inbound_identity.return_value = IdentityResolution(
        identity=identity,
        session_id="session-b",
    )
    manager.event_callback = AsyncMock()
    mock_adapter = make_adapter(channel_type="telegram")

    with patch(
        "gobby.communications.manager.get_adapter_class",
        return_value=MagicMock(return_value=mock_adapter),
    ):
        await manager.start()

    inbound = TelegramAdapter().parse_webhook(
        {
            "update_id": 10001,
            "message": {
                "message_id": 1003,
                "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
                "chat": {"id": 2222222, "type": "private"},
                "date": 1441645532,
                "text": "Reply for session A",
                "reply_to_message": {"message_id": 1001},
            },
        },
        {},
    )
    stored = await manager.handle_inbound_messages(channel.name, inbound)

    assert len(stored) == 1
    assert stored[0].content == "Reply for session A"
    assert stored[0].metadata_json["reply_to_message_id"] == "1001"
    assert stored[0].identity_id == identity.id
    assert stored[0].session_id == "session-a"
    assert stored[0].metadata_json["session_route"] == "reply"
    store.get_message_by_platform_id.assert_any_call(
        channel.name,
        "1001",
        platform_destination="2222222",
    )
    manager.event_callback.assert_awaited_once_with(
        "comms.message_received",
        message=stored[0],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attached_telegram_plain_message_routes_to_live_holder_then_falls_back() -> None:
    channel = make_channel(channel_type="telegram", config_json={"allow_from": ["42"]})
    store = make_store([channel])
    store.get_message_by_platform_id.return_value = None
    store.create_message.side_effect = lambda message: message
    sessions = MagicMock()
    holder = MagicMock(id="11111111-1111-4111-8111-111111111111", status="active", source="claude")
    lane = MagicMock(id="44444444-4444-4444-8444-444444444444", status="active", source="codex")
    comms_session = MagicMock(id="comms-session", status="active", source="comms")
    sessions.get.side_effect = lambda session_id: (
        holder if session_id == holder.id else lane if session_id == lane.id else comms_session
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    manager._identity_manager = MagicMock()
    manager._identity_manager.resolve_inbound_identity.return_value = IdentityResolution(
        identity=CommsIdentity(
            id="identity",
            channel_id=channel.id,
            external_user_id="42",
            created_at=_FIXED_TS,
            updated_at=_FIXED_TS,
        ),
        session_id="comms-session",
    )
    manager.event_callback = AsyncMock()
    mailbox = MagicMock(send=AsyncMock(return_value=MailboxSendResult()))
    manager.set_telegram_action_controller(TelegramActionController(manager, sessions, mailbox))

    def inbound(reply_to: str | None = None) -> CommsMessage:
        return CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="inbound",
            content="hello",
            identity_id="42",
            metadata_json={
                "chat_id": "99",
                "conversation_type": "private",
                **({"reply_to_message_id": reply_to} if reply_to is not None else {}),
            },
            created_at=_FIXED_TS,
        )

    manager.attach_conversation(channel.name, "dm:99", holder.id)
    first = await manager.handle_inbound_messages(channel.name, [inbound()])
    assert first[0].session_id == holder.id
    assert first[0].metadata_json["session_route"] == "attached"
    manager.event_callback.assert_not_awaited()
    mailbox.send.assert_awaited_once()
    assert mailbox.send.await_args.kwargs["wake"] is True

    unsourced_reply = await manager.handle_inbound_messages(channel.name, [inbound("old-post")])
    assert unsourced_reply[0].session_id == holder.id
    assert mailbox.send.await_count == 2
    manager.event_callback.assert_not_awaited()

    manager.switch_conversation(channel.name, "dm:99", lane.id)
    switched = await manager.handle_inbound_messages(channel.name, [inbound()])
    later = await manager.handle_inbound_messages(channel.name, [inbound()])
    assert [switched[0].session_id, later[0].session_id] == [lane.id, lane.id]
    assert mailbox.send.await_count == 4

    manager.detach_conversation(channel.name, "dm:99", lane.id)
    second = await manager.handle_inbound_messages(channel.name, [inbound()])
    assert second[0].session_id == "comms-session"
    assert "session_route" not in second[0].metadata_json
    manager.event_callback.assert_awaited_once()

    manager.attach_conversation(channel.name, "dm:99", holder.id)
    holder.status = "expired"
    third = await manager.handle_inbound_messages(channel.name, [inbound()])
    assert third[0].session_id == "comms-session"
    assert manager.attached_session(channel.id, "dm:99") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attachment_sets_outbound_destination_and_rejects_second_holder() -> None:
    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    sessions.get.side_effect = lambda session_id: MagicMock(
        id=session_id, status="active", source="claude"
    )
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel
    manager._identity_manager = MagicMock()
    manager._identity_manager.get_identity_by_session.return_value = None

    manager.attach_conversation(channel.name, "topic:99:7", "session-a")
    with pytest.raises(ValueError, match="already attached"):
        manager.attach_conversation(channel.name, "topic:99:7", "session-b")
    metadata = await manager._enrich_outbound_metadata(channel, channel.name, "session-a", None)
    assert metadata["platform_destination"] == "99"
    assert metadata["thread_id"] == "7"


@pytest.mark.unit
def test_concurrent_attachment_claims_have_one_holder() -> None:
    channel = make_channel(channel_type="telegram")
    barrier = threading.Barrier(2)
    local = threading.local()

    def get_session(session_id: str) -> MagicMock:
        if not getattr(local, "entered", False):
            local.entered = True
            barrier.wait(timeout=5)
        return MagicMock(id=session_id, status="active", source="claude")

    sessions = MagicMock()
    sessions.get.side_effect = get_session
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel

    def claim(session_id: str) -> bool:
        try:
            manager.attach_conversation(channel.name, "dm:99", session_id)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("session-a", "session-b")))
    assert results.count(True) == 1
    assert results.count(False) == 1


def test_switch_telegram_conversation_replaces_holder_atomically() -> None:
    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    sessions.get.side_effect = lambda session_id: MagicMock(
        id=session_id, status="active", source="claude"
    )
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel
    manager.attach_conversation(channel.name, "dm:99", "assistant")

    manager.switch_conversation(channel.name, "dm:99", "lane")

    assert manager.attached_session(channel.id, "dm:99") == "lane"
    assert manager.attached_destination(channel.id, "assistant") is None
    assert manager.attached_destination(channel.id, "lane") == "dm:99"


async def test_telegram_target_survives_manager_restart(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    sessions = SessionManager(temp_db)
    target = sessions.register(
        external_id="telegram-target-restart",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Lane 4",
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(
        channel_type="telegram", channel_id="33333333-3333-4333-8333-333333333333"
    )
    store.create_channel(channel)
    first = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    first._channel_by_name[channel.name] = channel

    first.switch_conversation(channel.name, "dm:99", target.id)

    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.config_json["telegram_agent_targets"] == {"dm:99": target.id}
    restarted = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    with (
        patch.object(restarted._lifecycle, "ensure_gobby_chat_channel", AsyncMock()),
        patch.object(
            restarted._lifecycle,
            "init_adapter",
            AsyncMock(return_value=make_adapter(channel_type="telegram")),
        ),
    ):
        await restarted.start()
    assert restarted.attached_session(channel.id, "dm:99") == target.id
    await restarted.stop()


@pytest.mark.parametrize("child_depth", [0, 1])
@pytest.mark.parametrize("end_first", [False, True])
async def test_native_clear_transfers_dm_only_to_same_depth_successor(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    child_depth: int,
    end_first: bool,
) -> None:
    from typing import cast

    from gobby.hooks.event_handlers import EventHandlers
    from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
    from gobby.hooks.session_types import HookSessionManager
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    context = {
        "parent_pid": 4242,
        "parent_create_time": 1234.5,
        "gobby_pane_ref": "native-clear-pane",
    }
    predecessor = sessions.register(
        external_id="native-clear-predecessor",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        title="Lane 3",
        terminal_context=context,
    )
    # An unrelated Assistant must not take precedence over the linked successor.
    sessions.register(
        external_id="native-clear-other-assistant",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        title="Assistant",
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_type="telegram", channel_id=str(uuid.uuid4()))
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", predecessor.id)

    handlers = EventHandlers(
        session_manager=cast(HookSessionManager, sessions),
        get_machine_id=lambda: predecessor.machine_id,
        resolve_project_id=lambda _project, _cwd: sample_project["id"],
    )
    if end_first:
        handlers.handle_session_end(
            HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id=predecessor.external_id,
                source=SessionSource.CLAUDE,
                timestamp=_FIXED_TS,
                machine_id=predecessor.machine_id,
                data={"reason": "clear"},
                metadata={"_platform_session_id": predecessor.id},
            )
        )
        ended = sessions.get(predecessor.id)
        assert ended is not None
        assert ended.status == "expired"
        await manager.handle_session_status_transition(SessionStatusTransition.from_session(ended))
        assert manager._conversation_attachments[(channel.id, "dm:42")] == predecessor.id

    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="native-clear-successor",
        source=SessionSource.CLAUDE,
        timestamp=_FIXED_TS,
        machine_id=predecessor.machine_id,
        data={"source": "clear", "terminal_context": context, "agent_depth": child_depth},
    )
    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(handlers, "_activate_default_agent"),
        patch.object(handlers, "_compose_session_response", return_value=HookResponse()),
        patch(
            "gobby.hooks.terminal_context.psutil.Process",
            return_value=MagicMock(
                **{"name.return_value": "claude", "create_time.return_value": 1234.5}
            ),
        ),
        patch(
            "gobby.hooks.event_handlers._session_start.profile.read_user_profile_content",
            return_value="",
        ),
    ):
        response = handlers.handle_session_start(event)
    assert response.decision == "allow"
    successor = sessions.find_by_external_id(
        "native-clear-successor", sample_project["id"], "claude"
    )
    assert successor is not None
    assert successor.parent_session_id == (predecessor.id if child_depth == 0 else None)
    for transition in transitions:
        await manager.handle_session_status_transition(transition)

    expected_holder = successor.id if child_depth == 0 else predecessor.id
    assert manager._conversation_attachments[(channel.id, "dm:42")] == expected_holder
    assert manager.attached_session(channel.id, "dm:42") == (
        successor.id if child_depth == 0 else None
    )
    assert manager.list_conversation_attachments() == (
        [{"channel": channel.name, "conversation_id": "dm:42", "session_id": successor.id}]
        if child_depth == 0
        else []
    )
    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.config_json["telegram_agent_targets"] == {"dm:42": expected_holder}
    adapter.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"parent_pid": 43}, False),
        ({"parent_pid": None}, False),
        ({"parent_create_time": 1235.5}, False),
        ({"parent_create_time": None}, False),
    ],
)
def test_native_clear_matching_requires_cli_process_identity(
    changes: dict[str, Any], expected: bool
) -> None:
    from gobby.sessions.handoff_identity import terminal_process_contexts_match

    context = {"parent_pid": 42, "parent_create_time": 1234.5, "gobby_pane_ref": "clear-pane"}
    assert terminal_process_contexts_match(context, {**context, **changes}) is expected


async def test_native_clear_does_not_take_sibling_panes_dm(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.sessions.handoff_identity import terminal_process_contexts_match

    sessions = SessionManager(temp_db)
    shared_context = {
        "tmux_session": "operator-panes",
        "tmux_socket_path": "/tmp/native-clear-test.sock",
    }
    sibling = sessions.register(
        external_id="native-clear-sibling",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        terminal_context={
            **shared_context,
            "parent_pid": 4242,
            "parent_create_time": 1234.5,
            "gobby_pane_ref": "sibling-pane",
        },
    )
    successor = sessions.register(
        external_id="native-clear-other-pane",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        terminal_context={
            **shared_context,
            "parent_pid": 4243,
            "parent_create_time": 1235.5,
            "gobby_pane_ref": "clear-pane",
        },
    )
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_type="telegram", channel_id=str(uuid.uuid4()))
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    manager.attach_conversation(channel.name, "dm:42", sibling.id)

    assert not terminal_process_contexts_match(sibling.terminal_context, successor.terminal_context)
    assert sessions.bind_native_clear_successor(successor.id) is None
    refreshed_sibling = sessions.get(sibling.id)
    refreshed_successor = sessions.get(successor.id)
    assert refreshed_sibling is not None and refreshed_sibling.status == "active"
    assert refreshed_successor is not None and refreshed_successor.parent_session_id is None
    assert manager.attached_session(channel.id, "dm:42") == sibling.id
    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.config_json["telegram_agent_targets"] == {"dm:42": sibling.id}


@pytest.mark.parametrize("end", ["deleted", "staged-clear", "consumed-clear"])
@pytest.mark.parametrize("assistant_available", [True, False])
@pytest.mark.parametrize("linked_successor", [False, True])
async def test_ended_target_falls_back_to_real_assistant_clear_successor(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    end: str,
    assistant_available: bool,
    linked_successor: bool,
) -> None:
    from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
    from gobby.sessions.status_events import SessionStatusTransition
    from gobby.workflows.state_manager import SessionVariableManager

    sessions = SessionManager(temp_db)
    target = sessions.register(
        external_id="telegram-ended-target",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Lane 4",
    )
    predecessor = sessions.register(
        external_id="telegram-assistant-predecessor",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Assistant",
    )
    successor = sessions.register(
        external_id="telegram-assistant-successor",
        machine_id=None,
        source="codex",
        project_id=sample_project["id"],
        title="Lane 4 continued" if linked_successor else "Assistant - Main",
        parent_session_id=target.id if linked_successor else predecessor.id,
        agent_depth=0,
    )
    if not linked_successor or not assistant_available:
        assert sessions.update_session_status(predecessor.id, "expired")
    if not assistant_available:
        assert sessions.update_session_status(successor.id, "expired")
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(
        channel_type="telegram", channel_id="33333333-3333-4333-8333-333333333335"
    )
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.switch_conversation(channel.name, "dm:99", target.id)
    if end != "deleted":
        SessionVariableManager(temp_db).set_variable(
            target.id,
            CLEAR_ATTEMPT_VARIABLE,
            {
                "attempt_id": "clear-attempt",
                "consumed_by": successor.id if end == "consumed-clear" else None,
            },
        )
    assert sessions.update_session_status(target.id, "deleted" if end == "deleted" else "expired")

    await manager.handle_session_status_transition(
        SessionStatusTransition(
            session_id=target.id,
            project_id=sample_project["id"],
            agent_run_id=None,
            status="deleted" if end == "deleted" else "expired",
            transitioned_at=_FIXED_TS,
            seq_num=target.seq_num,
            title="Lane 4",
            source="codex",
        )
    )

    stored = store.get_channel(channel.id)
    assert stored is not None
    if assistant_available:
        expected_holder = predecessor.id if linked_successor else successor.id
        assert manager.attached_session(channel.id, "dm:99") == expected_holder
        assert stored.config_json["telegram_agent_targets"] == {"dm:99": expected_holder}
        adapter.send_message.assert_not_awaited()
    else:
        assert manager.attached_session(channel.id, "dm:99") is None
        assert stored.config_json["telegram_agent_targets"] == {}
        adapter.send_message.assert_awaited_once()
        assert "No Assistant is running" in adapter.send_message.await_args.args[0].content


def test_atomic_channel_config_update_preserves_telegram_target(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(
        channel_type="telegram",
        channel_id="33333333-3333-4333-8333-333333333334",
        config_json={"bot_token": "$secret:telegram-token"},
    )
    store.create_channel(channel)
    store.set_telegram_agent_target(channel.id, "dm:99", "11111111-1111-4111-8111-111111111111")

    store.merge_channel_config(channel.id, {"poll_offset": 501})

    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.config_json == {
        "bot_token": "$secret:telegram-token",
        "telegram_agent_targets": {"dm:99": "11111111-1111-4111-8111-111111111111"},
        "poll_offset": 501,
    }


async def test_ended_target_falls_back_to_live_assistant() -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    target = MagicMock(id="11111111-1111-4111-8111-111111111111", status="active")
    assistant = MagicMock(
        id="22222222-2222-4222-8222-222222222222",
        status="active",
        source="claude",
        title="Assistant",
        agent_depth=0,
        agent_run_id=None,
    )
    sessions.get.side_effect = lambda session_id: (
        target if session_id == target.id else assistant if session_id == assistant.id else None
    )
    sessions.list.return_value = [assistant]
    store = make_store([channel])
    store.get_channel.return_value = channel
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    manager.switch_conversation(channel.name, "dm:99", target.id)
    target.status = "expired"

    await manager.handle_session_status_transition(
        SessionStatusTransition(
            session_id=target.id,
            project_id="project-1",
            agent_run_id=None,
            status="expired",
            transitioned_at=_FIXED_TS,
            seq_num=1,
            title="Lane Developer",
            source="claude",
        )
    )

    assert manager.attached_session(channel.id, "dm:99") == assistant.id
    assert manager.attached_destination(channel.id, target.id) is None
    assert manager.attached_destination(channel.id, assistant.id) == "dm:99"


async def test_ended_target_with_no_assistant_replies_without_switching() -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    channel = make_channel(channel_type="telegram")
    store = make_store([channel])
    store.get_channel.return_value = channel
    sessions = MagicMock()
    target = MagicMock(id="11111111-1111-4111-8111-111111111111", status="active")
    sessions.get.side_effect = lambda session_id: target if session_id == target.id else None
    sessions.list.return_value = []
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.switch_conversation(channel.name, "dm:99", target.id)
    target.status = "expired"

    await manager.handle_session_status_transition(
        SessionStatusTransition(
            session_id=target.id,
            project_id="project-1",
            agent_run_id=None,
            status="expired",
            transitioned_at=_FIXED_TS,
            seq_num=1,
            title="Lane Developer",
            source="claude",
        )
    )

    assert manager.attached_session(channel.id, "dm:99") is None
    assert "No Assistant is running" in adapter.send_message.await_args.args[0].content


async def test_group_attachment_does_not_fall_back_to_assistant() -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    channel = make_channel(channel_type="telegram")
    store = make_store([channel])
    store.get_channel.return_value = channel
    target = MagicMock(id="11111111-1111-4111-8111-111111111111", status="active")
    assistant = MagicMock(
        id="22222222-2222-4222-8222-222222222222",
        status="active",
        source="claude",
        title="Assistant",
        agent_depth=0,
        agent_run_id=None,
    )
    sessions = MagicMock()
    sessions.get.side_effect = lambda session_id: (
        target if session_id == target.id else assistant if session_id == assistant.id else None
    )
    sessions.list.return_value = [assistant]
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    manager.attach_conversation(channel.name, "group:99", target.id)
    target.status = "expired"

    await manager.handle_session_status_transition(
        SessionStatusTransition(
            session_id=target.id,
            project_id="project-1",
            agent_run_id=None,
            status="expired",
            transitioned_at=_FIXED_TS,
            seq_num=1,
            title="Lane 4",
            source="codex",
        )
    )

    assert manager._conversation_attachments.get((channel.id, "group:99")) is None
    assert manager.attached_destination(channel.id, assistant.id) is None


async def test_telegram_outbound_names_active_and_other_agent_senders() -> None:
    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    assistant = MagicMock(
        id="11111111-1111-4111-8111-111111111111",
        status="active",
        source="claude",
        title="Assistant",
        ref="gobby#15976",
    )
    lane = MagicMock(
        id="44444444-4444-4444-8444-444444444444",
        status="active",
        source="codex",
        title="Lane Developer",
        ref="gobby#16004",
    )
    sessions.get.side_effect = lambda session_id: (
        assistant if session_id == assistant.id else lane if session_id == lane.id else None
    )
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel
    manager._adapters[channel.name] = make_adapter(channel_type="telegram")
    manager._identity_manager = MagicMock()
    manager._identity_manager.get_identity_by_session.return_value = None
    manager.attach_conversation(channel.name, "dm:99", assistant.id)

    active = await manager.send_message(
        channel.name, "Hello", session_id=assistant.id, metadata={"platform_destination": "99"}
    )
    other = await manager.send_message(
        channel.name, "Update", session_id=lane.id, metadata={"platform_destination": "99"}
    )

    assert active.content == "Hello"
    assert other.content == "Update"
    assert active.metadata_json["telegram_sender_label"] == "Assistant (gobby#15976)"
    assert other.metadata_json["telegram_sender_label"] == "Lane Developer (gobby#16004)"


async def test_telegram_stream_edit_keeps_the_agent_name() -> None:
    channel = make_channel(channel_type="telegram")
    store = make_store([channel])
    session_id = "11111111-1111-4111-8111-111111111111"
    stored_message = CommsMessage(
        id="outbound-1",
        channel_id=channel.id,
        direction="outbound",
        content="Assistant: partial",
        session_id=session_id,
        created_at=_FIXED_TS,
    )
    store.get_message_by_platform_id.return_value = stored_message
    store.update_message_content.side_effect = lambda _id, content: setattr(
        stored_message, "content", content
    )
    sessions = MagicMock()
    sessions.get.return_value = MagicMock(
        id=session_id, status="active", source="claude", title="Assistant", ref="gobby#15976"
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    adapter.supports_message_edit = True
    adapter.edit_message = AsyncMock()
    manager._adapters[channel.name] = adapter

    await manager.edit_message(channel.name, "platform-1", "Finished", "99")

    adapter.edit_message.assert_awaited_once_with(
        "platform-1", "Finished", "99", sender_label="Assistant (gobby#15976)"
    )
    assert stored_message.content == "Finished"
    store.update_message_content.assert_called_once_with("outbound-1", "Finished")
    store.get_message_by_platform_id.assert_called_once_with(
        channel.name, "platform-1", platform_destination="99"
    )
    store.supersede_callback_decision.assert_not_called()


@pytest.mark.parametrize(
    ("seat", "content", "keyboard"),
    [
        ("Assistant", "Explicit post", None),
        ("Assistant", "Ship?", [[{"text": "Approve", "value": "approve"}]]),
        ("Reporter", "Systems nominal", None),
        ("Watchdog", "Agent stalled", None),
        ("Monitor", "Load alarm", None),
    ],
)
async def test_telegram_origin_kinds_share_trusted_heading(
    seat: str, content: str, keyboard: list[list[dict[str, str]]] | None
) -> None:
    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    sessions.get.return_value = MagicMock(
        source="codex", title=seat, ref="gobby#42", status="active", transcript_path=None
    )
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    sent = await manager.send_message(
        channel.name,
        content,
        session_id="origin",
        metadata={
            "platform_destination": "99",
            "telegram_sender_label": "Spoofed",
            "inline_keyboard": keyboard,
        },
    )
    assert sent.status == "sent"
    delivered = adapter.send_message.call_args.args[0]
    assert delivered.metadata_json["telegram_sender_label"] == f"{seat} (gobby#42)"
    assert delivered.metadata_json["inline_keyboard"] == keyboard
    assert delivered.content == content


async def test_telegram_mirror_labels_attached_origin() -> None:
    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    sessions.get.return_value = MagicMock(
        source="claude", title="Assistant", ref="gobby#15976", status="active", transcript_path=None
    )
    store = make_store([channel])
    store.reserve_transcript_message.side_effect = lambda message: (message, True)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:99", "origin")
    await manager.mirror_session_message("origin", "reply-id", "Completed reply")
    delivered = adapter.send_message.call_args.args[0]
    assert delivered.metadata_json["telegram_sender_label"] == "Assistant (gobby#15976)"
    assert delivered.metadata_json["platform_destination"] == "99"
    assert delivered.session_id == "origin"
    assert delivered.content == "Completed reply"


async def test_sessionless_telegram_alarm_is_labeled() -> None:
    channel = make_channel(channel_type="telegram")
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), MagicMock()
    )
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    sent = await manager.send_message(
        channel.name, "Load alarm", metadata={"platform_destination": "99"}
    )
    assert sent.status == "sent"
    assert adapter.send_message.call_args.args[0].metadata_json["telegram_sender_label"] == (
        "System (gobby)"
    )


async def test_telegram_button_edit_resolves_origin_instead_of_stale_metadata() -> None:
    from gobby.communications.telegram_decisions import _publish

    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    sessions.get.return_value = MagicMock(source="claude", title="Assistant", ref="gobby#15976")
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel
    adapter = MagicMock(spec=TelegramAdapter)
    adapter.edit_stored_message = AsyncMock()
    source = CommsMessage(
        id="decision",
        channel_id=channel.id,
        direction="outbound",
        content="Ship?",
        session_id="origin",
        metadata_json={"telegram_sender_label": "Old label"},
        created_at=_FIXED_TS,
    )
    keyboard = [[{"text": "Approve", "value": "approve"}]]
    await _publish(manager, adapter, source, "Ship now?", "99", keyboard, 1)
    call = adapter.edit_stored_message.await_args
    assert call.args == (source, "Ship now?", "99")
    assert call.kwargs["sender_label"] == "Assistant (gobby#15976)"
    assert call.kwargs["inline_keyboard"] == keyboard
    assert call.kwargs["callback_generation"] == 1


@pytest.mark.parametrize("source", ["comms", "web-chat", "web_chat", "system"])
@pytest.mark.parametrize(
    "title", ["Comms: operator-user", "Comms group: -100123", "Comms topic: -100123/77"]
)
def test_telegram_non_seat_sessions_never_label_operator_identity(source: str, title: str) -> None:
    sessions = MagicMock()
    sessions.get.return_value = MagicMock(source=source, title=title, ref="gobby#42")
    manager = CommunicationsManager(make_config(), make_store(), make_secret_store(), sessions)
    channel = make_channel(channel_type="telegram")
    expected = "System (gobby#42)" if source == "system" else "Responder (gobby#42)"
    label = manager.telegram_sender_label(channel, "identity-session")
    assert label == expected
    assert title not in label


async def test_unattached_chat_responder_labels_bot_instead_of_operator() -> None:
    from gobby.communications.chat_transport import CommunicationsChatStreamTransport
    from gobby.communications.responder import ResponderContext

    channel = make_channel(channel_type="telegram")
    sessions = MagicMock()
    sessions.get.return_value = MagicMock(
        source="comms", title="Comms: operator-user", ref="gobby#42", transcript_path=None
    )
    manager = CommunicationsManager(
        make_config(), make_store([channel]), make_secret_store(), sessions
    )
    manager._channel_by_name[channel.name] = channel
    adapter = TelegramAdapter()
    client = MagicMock()
    client.post = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("POST", "https://api.telegram.org/bottest/sendMessage"),
            json={"ok": True, "result": {"message_id": 1}},
        )
    )
    adapter._client = client
    adapter._api_base = "https://api.telegram.org/bottest"
    manager._adapters[channel.name] = adapter
    context = ResponderContext(
        channel=channel,
        message=CommsMessage(
            id="inbound",
            channel_id=channel.id,
            direction="inbound",
            content="Hello",
            session_id="identity-session",
            created_at=_FIXED_TS,
        ),
        conversation_id="99",
        sender_id="operator-user",
        is_group=False,
        responder_config={},
    )
    transport = CommunicationsChatStreamTransport(manager, context)
    delivered = await transport._send("Hello back")
    assert delivered.session_id == "identity-session"
    payload = client.post.await_args.kwargs["json"]
    assert payload["text"] == "<b>Responder (gobby#42):</b>\nHello back"
    assert "operator-user" not in payload["text"]


async def test_telegram_edit_supersedes_or_replaces_decision_keyboard(
    temp_db: HubDatabase,
) -> None:
    channel = make_channel(
        channel_id="00000000-0000-0000-0000-000000000201", channel_type="telegram"
    )
    store = LocalCommunicationsStore(temp_db, project_id="00000000-0000-0000-0000-000000000000")
    store.create_channel(channel)
    decision_id = "00000000-0000-0000-0000-000000000202"
    store.create_message(
        CommsMessage(
            id=decision_id,
            channel_id=channel.id,
            direction="outbound",
            content="Approve?",
            platform_message_id="platform-1",
            metadata_json={
                "platform_destination": "99",
                "inline_keyboard": [[{"text": "Approve", "value": "approve"}]],
            },
            created_at=datetime.now(UTC),
        )
    )
    manager = CommunicationsManager(make_config(), store, make_secret_store(), MagicMock())
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    adapter.supports_message_edit = True
    adapter.edit_stored_message = AsyncMock(return_value=["platform-1"])
    manager._adapters[channel.name] = adapter
    replacement = [[{"text": "Ship", "value": "ship"}]]

    await manager.edit_message(
        channel.name, "platform-1", "Approve v2?", "99", inline_keyboard=replacement
    )
    replaced = store.get_message(decision_id)
    assert replaced is not None
    assert replaced.metadata_json["inline_keyboard"] == replacement
    assert replaced.metadata_json.get("callback_state") is None
    assert replaced.content == "Approve v2?"

    await manager.edit_message(channel.name, "platform-1", "Withdrawn", "99")
    withdrawn = store.get_message(decision_id)
    assert withdrawn is not None
    assert withdrawn.metadata_json["callback_state"] == "superseded"


@pytest.mark.unit
async def test_send_attachment_telegram_ok_false_is_failed_not_sent(tmp_path: Path) -> None:
    """A sendDocument reply with ok=false stores a failed message, never a sent one."""
    channel = make_channel(channel_type="telegram")
    manager = CommunicationsManager(make_config(), make_store(), make_secret_store(), MagicMock())
    adapter = TelegramAdapter()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    response = httpx.Response(
        200,
        request=httpx.Request("POST", f"{adapter._api_base}/sendDocument"),
        json={"ok": False, "description": "Bad Request: chat not found"},
    )
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    adapter._client = client
    manager._adapters[channel.name] = adapter
    manager._channel_by_name[channel.name] = channel
    file_path = tmp_path / "errors-new.txt"
    file_path.write_text("boom\n", encoding="utf-8")

    with patch.object(
        manager._outbound, "enrich_metadata", return_value={"platform_destination": "chat999"}
    ):
        message, _ = await manager.send_attachment(
            channel.name, file_path, content_type="text/plain"
        )

    assert message.status == "failed"
    assert message.platform_message_id is None
    assert message.error == "Telegram sendDocument failed: Bad Request: chat not found"


def _start_context_session(
    sessions: SessionManager,
    project_id: str,
    machine_id: str,
    external_id: str,
    context: dict[str, Any],
    *,
    source: str = "startup",
) -> str:
    """Exercise SessionStart with real storage and isolated runtime boundaries."""
    from gobby.hooks.event_handlers import EventHandlers
    from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
    from gobby.hooks.session_types import HookSessionManager

    handlers = EventHandlers(
        session_manager=cast(HookSessionManager, sessions),
        get_machine_id=lambda: machine_id,
        resolve_project_id=lambda _project, _cwd: project_id,
    )
    with (
        patch.object(handlers, "_derive_transcript_path", return_value=None),
        patch.object(handlers, "_setup_code_index"),
        patch.object(handlers, "_activate_default_agent"),
        patch.object(handlers, "_compose_session_response", return_value=HookResponse()),
        patch(
            "gobby.hooks.terminal_context.psutil.Process",
            return_value=MagicMock(
                **{
                    "name.return_value": "claude",
                    "create_time.return_value": context["parent_create_time"],
                }
            ),
        ),
        patch(
            "gobby.hooks.event_handlers._session_start.profile.read_user_profile_content",
            return_value="",
        ),
    ):
        response = handlers.handle_session_start(
            HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id=external_id,
                source=SessionSource.CLAUDE,
                timestamp=_FIXED_TS,
                machine_id=machine_id,
                data={"source": source, "terminal_context": context},
            )
        )
    assert response.decision == "allow"
    registered = sessions.find_by_external_id(external_id, project_id, "claude")
    assert registered is not None
    return registered.id


@pytest.mark.parametrize("status", ["active", "paused"])
@pytest.mark.parametrize("reuse", ["sibling", "same-pane", "restarted-pane", "restarted-tty"])
async def test_context_reuse_session_start_preserves_sibling_dm(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    status: str,
    reuse: str,
) -> None:
    from gobby.communications.telegram_fallback import fallback_telegram_targets
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    context = {
        "tmux_pane": "%1",
        "tmux_session": "fleet",
        "tmux_socket_path": "/tmp/test-context-reuse/socket",
        "parent_pid": 4242,
        "parent_create_time": 1234.5,
    }
    predecessor = sessions.register(
        external_id="context-reuse-predecessor",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    sessions.update_status(predecessor.id, status)
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_type="telegram", channel_id=str(uuid.uuid4()))
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = make_adapter(channel_type="telegram")
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", predecessor.id)
    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)

    next_context = dict(context)
    same_terminal = reuse != "sibling"
    if reuse != "same-pane":
        next_context.update(parent_pid=4343, parent_create_time=2345.6)
    if reuse == "sibling":
        next_context["tmux_pane"] = "%2"
    elif reuse == "restarted-tty":
        context = {"tty": "/dev/test-reused", "parent_pid": 4242, "parent_create_time": 1234.5}
        sessions.update(predecessor.id, terminal_context=context)
        next_context = {"tty": "/dev/test-reused", "parent_pid": 4343, "parent_create_time": 2345.6}
    # Bootstrap recovery materializes startup rows before SessionStart activation.
    sessions.register(
        external_id="context-reuse-newcomer",
        machine_id=predecessor.machine_id,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=next_context,
    )
    _start_context_session(
        sessions,
        sample_project["id"],
        predecessor.machine_id,
        "context-reuse-newcomer",
        next_context,
    )
    persisted = sessions.get(predecessor.id)
    assert persisted is not None
    assert persisted.status == ("expired" if same_terminal else status)
    ended = [t for t in transitions if t.session_id == predecessor.id and t.status == "expired"]
    assert bool(ended) is same_terminal
    with patch(
        "gobby.communications.telegram_fallback.fallback_telegram_targets",
        wraps=fallback_telegram_targets,
    ) as fallback:
        for transition in transitions:
            await manager.handle_session_status_transition(transition)
        if same_terminal:
            fallback.assert_awaited_once()
        else:
            fallback.assert_not_awaited()
            assert manager.attached_session(channel.id, "dm:42") == predecessor.id
            stored = store.get_channel(channel.id)
            assert stored is not None
            assert stored.config_json["telegram_agent_targets"] == {"dm:42": predecessor.id}
            adapter.send_message.assert_not_awaited()


async def test_resumed_older_session_native_clear_transfers_its_dm(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.sessions.status_events import SessionStatusTransition

    sessions = SessionManager(temp_db)
    context = {"tty": "/dev/test-resume", "parent_pid": 4242, "parent_create_time": 1234.5}
    older = sessions.register(
        external_id="resumed-older",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    newer = sessions.register(
        external_id="pre-resume-newer",
        machine_id=older.machine_id,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    assert older.created_at < newer.created_at
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    channel = make_channel(channel_type="telegram", channel_id=str(uuid.uuid4()))
    store.create_channel(channel)
    manager = CommunicationsManager(make_config(), store, make_secret_store(), sessions)
    manager._channel_by_name[channel.name] = channel
    manager._adapters[channel.name] = make_adapter(channel_type="telegram")
    manager.attach_conversation(channel.name, "dm:42", older.id)
    transitions: list[SessionStatusTransition] = []
    sessions.register_status_transition_listener(transitions.append)

    resumed_id = _start_context_session(
        sessions,
        sample_project["id"],
        older.machine_id,
        older.external_id,
        context,
        source="resume",
    )
    assert resumed_id == older.id
    resumed = sessions.get(older.id)
    prior = sessions.get(newer.id)
    assert resumed is not None and prior is not None
    assert resumed.created_at == older.created_at
    assert resumed.last_activity is not None and prior.last_activity is not None
    assert resumed.last_activity > prior.last_activity
    for transition in transitions:
        await manager.handle_session_status_transition(transition)
    transitions.clear()
    assert manager.attached_session(channel.id, "dm:42") == older.id

    successor_id = _start_context_session(
        sessions,
        sample_project["id"],
        older.machine_id,
        "resumed-clear-successor",
        context,
        source="clear",
    )
    successor = sessions.get(successor_id)
    assert successor is not None
    assert successor.parent_session_id == older.id
    for transition in transitions:
        await manager.handle_session_status_transition(transition)
    assert manager.attached_session(channel.id, "dm:42") == successor.id
    stored = store.get_channel(channel.id)
    assert stored is not None
    assert stored.config_json["telegram_agent_targets"] == {"dm:42": successor.id}


def test_native_clear_failed_expiry_resets_parent_for_retry(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions = SessionManager(temp_db)
    context = {"tty": "/dev/test-retry", "parent_pid": 4242, "parent_create_time": 1234.5}
    predecessor = sessions.register(
        external_id="retry-predecessor",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    successor = sessions.register(
        external_id="retry-successor",
        machine_id=predecessor.machine_id,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    with patch.object(sessions, "mark_session_expired", return_value=False):
        assert sessions.bind_native_clear_successor(successor.id) is None
    unbound = sessions.get(successor.id)
    assert unbound is not None
    assert unbound.parent_session_id is None
    live = sessions.get(predecessor.id)
    assert live is not None and live.status == "active"

    ended = sessions.bind_native_clear_successor(successor.id)
    assert ended is not None and ended.id == predecessor.id and ended.status == "expired"
    bound = sessions.get(successor.id)
    assert bound is not None and bound.parent_session_id == predecessor.id

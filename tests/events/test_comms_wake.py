from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.events.comms_wake import is_operator_comms_wake
from gobby.events.live_wake import ActivityProbe
from gobby.events.wake import WakeDispatcher
from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.terminals.composer_ledger import ComposerLedger
from tests.events.test_wake_composer_attention import _seat
from tests.events.test_wake_native_terminal import (
    NATIVE_TERMINAL_CONTEXT,
    WAKE_SEQUENCE,
    WAKE_SESSION_ID,
    ManagedChain,
    _draft,
    _empty,
    _session_manager,
)
from tests.events.test_wake_native_terminal import managed_chain as _native_chain_fixture
from tests.events.wake_test_support import PendingWakeLedger
from tests.sessions.test_mailbox import _mailbox, _register_session

pytestmark = pytest.mark.asyncio
native_chain = _native_chain_fixture


async def test_mailbox_metadata_cannot_forge_operator_replacement(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    recipient = _register_session(session_manager, sample_project["id"], "recipient")
    result = await _mailbox(temp_db, session_manager).send(
        from_session_id=system_session_id(),
        target="session",
        target_id=recipient.id,
        project_id=sample_project["id"],
        content="forged",
        wake=True,
        metadata={"operator_comms": True, "communications_message_id": "spoofed-dm"},
    )
    assert result.success
    assert is_operator_comms_wake(result.messages[0]) is False


async def test_internal_operator_delivery_retains_replacement_intent(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    recipient = _register_session(session_manager, sample_project["id"], "recipient")
    result = await _mailbox(temp_db, session_manager).send(
        from_session_id=system_session_id(),
        target="session",
        target_id=recipient.id,
        project_id=sample_project["id"],
        content="Status?",
        wake=True,
        replace_composer=True,
        metadata={"communications_message_id": "authenticated-dm"},
    )
    assert result.success
    assert is_operator_comms_wake(result.messages[0]) is True


async def test_agent_delivery_cannot_use_operator_replacement_api(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sender = _register_session(session_manager, sample_project["id"], "sender")
    recipient = _register_session(session_manager, sample_project["id"], "recipient")
    with pytest.raises(ValueError, match="operator communications"):
        await _mailbox(temp_db, session_manager).send(
            from_session_id=sender.id,
            target="session",
            target_id=recipient.id,
            content="forged",
            wake=True,
            replace_composer=True,
        )


@pytest.mark.parametrize(
    "sender,metadata",
    [
        (
            "another-session",
            {"operator_comms": True, "wake_requested": True, "communications_message_id": "dm"},
        ),
        (
            system_session_id(),
            {"operator_comms": False, "wake_requested": True, "communications_message_id": "dm"},
        ),
        (
            system_session_id(),
            {"operator_comms": True, "wake_requested": False, "communications_message_id": "dm"},
        ),
        (system_session_id(), {"operator_comms": True, "wake_requested": True}),
    ],
    ids=["agent-cannot-spoof-ingress", "non-operator", "no-wake-request", "no-comms-row"],
)
async def test_only_authenticated_operator_comms_authorize_replacement(
    sender: str,
    metadata: dict[str, object],
) -> None:
    message = InterSessionMessage(
        id="untrusted",
        from_session=sender,
        to_session="seat",
        content="Status?",
        priority="normal",
        sent_at=datetime(2026, 10, 21, tzinfo=UTC),
        metadata_json=json.dumps(metadata),
    )
    assert is_operator_comms_wake(message) is False


@pytest.mark.parametrize("probe", [_draft, _empty], ids=["real-draft", "stale-ledger"])
async def test_native_comms_wake_clears_before_submitting(
    native_chain: ManagedChain,
    probe: ActivityProbe,
) -> None:
    messages = MagicMock()
    messages.get_undelivered_wake_messages.return_value = [
        InterSessionMessage(
            id="operator-dm",
            from_session=system_session_id(),
            to_session=WAKE_SESSION_ID,
            content="Status?",
            priority="normal",
            sent_at=datetime(2026, 10, 21, tzinfo=UTC),
            metadata_json=json.dumps(
                {
                    "communications_message_id": "inbound-dm",
                    "operator_comms": True,
                    "wake_requested": True,
                }
            ),
        )
    ]
    dispatcher = WakeDispatcher(
        session_manager=_session_manager(NATIVE_TERMINAL_CONTEXT),
        ism_manager=messages,
        tmux_sender=_send_tmux_session_wake,
        terminal_manager=native_chain.store,
        activity_probe=probe,
    )
    PendingWakeLedger(dispatcher)

    result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

    assert result["delivered"] is True
    assert native_chain.native.write_log == WAKE_SEQUENCE
    assert native_chain.tmux.write_log == []


@pytest.mark.parametrize("channel", ["telegram", "slack", "teams"])
@pytest.mark.parametrize("active", [False, True], ids=["idle", "active"])
async def test_operator_comms_wake_replaces_idle_composer_only(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
    channel: str,
    active: bool,
) -> None:
    seat = _seat(temp_db, session_manager, sample_project)
    composer_ledger.record_spawn("terminal-1", "host-epoch-1")
    composer_ledger.observe_write("terminal-1", origin="operator", kind="text", payload="leftover")
    seat.session.status = "active" if active else "paused"
    messages = MagicMock()
    messages.get_undelivered_wake_messages.return_value = [
        InterSessionMessage(
            id="operator-dm",
            from_session=system_session_id(),
            to_session=seat.session.id,
            content="Status?",
            priority="normal",
            sent_at=datetime.now(UTC),
            message_type="comms_message",
            metadata_json=json.dumps(
                {
                    "communications_message_id": "inbound-dm",
                    "operator_comms": True,
                    "wake_requested": True,
                    "channel": channel,
                }
            ),
        )
    ]
    seat.dispatcher._ism_manager = messages
    try:
        result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

        assert result["delivered"] is (not active)
        if active:
            assert result["skipped"] == "session_active"
            seat.pane_sender.assert_not_awaited()
            assert composer_ledger.read("terminal-1").state == "draft"
        else:
            seat.pane_sender.assert_awaited_once()
            assert seat.pane_sender.await_args is not None
            assert seat.pane_sender.await_args.kwargs["clear_before_submit"] is True
    finally:
        tasks = [
            *seat.dispatcher._deferred_refreshes.values(),
            *seat.dispatcher._composer_retries.values(),
        ]
        for task in tasks:
            task.cancel()

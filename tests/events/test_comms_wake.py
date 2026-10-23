from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.comms_wake import is_operator_comms_wake
from gobby.events.live_wake import (
    PANE_FOCUSED,
    RETRYABLE_WAKE_SKIPS,
    ActivityProbe,
    TerminalActivity,
)
from gobby.events.wake import WakeDispatcher
from gobby.runner_init.orchestration import _send_tmux_session_wake
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.terminals.composer_ledger import ComposerLedger
from gobby.terminals.leases import TerminalLeaseRegistry
from tests.events.test_wake_composer_attention import TERMINAL_ID, _seat
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
from tests.events.wake_test_support import (
    PendingWakeLedger,
    operator_comms_message,
    plain_message,
)
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


async def _focus_holder(
    dispatcher: WakeDispatcher,
    *,
    viewer: Literal["web", "gclient"] = "gclient",
    focused: bool = True,
) -> tuple[TerminalLeaseRegistry, str]:
    registry = TerminalLeaseRegistry()
    dispatcher.set_lease_registry(registry)
    record = await registry.attach(TERMINAL_ID, viewer=viewer)
    await registry.take_control(TERMINAL_ID, record.attachment_id)
    if focused:
        await registry.set_focus(record.attachment_id, True)
    return registry, record.attachment_id


def _ism_with(message: InterSessionMessage) -> MagicMock:
    messages = MagicMock()
    messages.get_undelivered_wake_messages.return_value = [message]
    messages.get_undelivered_messages.return_value = [message]
    return messages


def _cancel_background(dispatcher: WakeDispatcher) -> None:
    tasks = [
        *dispatcher._deferred_refreshes.values(),
        *dispatcher._composer_retries.values(),
        *dispatcher._focus_releases,
    ]
    for task in tasks:
        task.cancel()


async def test_operator_comms_wake_holds_while_the_pane_is_focused(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
) -> None:
    seat = _seat(temp_db, session_manager, sample_project)
    composer_ledger.record_spawn(TERMINAL_ID, "host-epoch-1")
    seat.session.status = "paused"
    seat.dispatcher._ism_manager = _ism_with(operator_comms_message(seat.session.id))
    await _focus_holder(seat.dispatcher)
    try:
        result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

        assert result.get("skipped") == PANE_FOCUSED
        assert result.get("delivered") is False
        assert PANE_FOCUSED in RETRYABLE_WAKE_SKIPS
        assert seat.session.id in seat.dispatcher._composer_retries
        seat.pane_sender.assert_not_awaited()
        assert seat.dispatcher._focus_holds == {TERMINAL_ID: {seat.session.id: "normal"}}
    finally:
        _cancel_background(seat.dispatcher)


async def test_focus_release_dispatches_the_held_wake_with_a_wipe(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = _seat(temp_db, session_manager, sample_project)
    composer_ledger.record_spawn(TERMINAL_ID, "host-epoch-1")
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind="text", payload="leftover")
    seat.session.status = "paused"
    seat.dispatcher._ism_manager = _ism_with(operator_comms_message(seat.session.id))

    async def parked_retry(delay: float) -> None:
        # Park the backoff retry so only the focus release can deliver the wake.
        await asyncio.Event().wait()

    monkeypatch.setattr(seat.dispatcher, "_composer_retry_wait", parked_retry)
    registry, attachment_id = await _focus_holder(seat.dispatcher)
    registry.set_focus_observer(seat.dispatcher.release_focus_hold)
    try:
        held = await seat.dispatcher.dispatch_live_wake(seat.session.id)
        assert held.get("skipped") == PANE_FOCUSED
        seat.pane_sender.assert_not_awaited()

        await registry.set_focus(attachment_id, False)
        results = await asyncio.gather(*list(seat.dispatcher._focus_releases))

        assert [result["delivered"] for result in results] == [True]
        seat.pane_sender.assert_awaited_once()
        assert seat.pane_sender.await_args is not None
        assert seat.pane_sender.await_args.kwargs["clear_before_submit"] is True

        # The hold was consumed, so a second release dispatches nothing.
        seat.dispatcher.release_focus_hold(TERMINAL_ID)
        await asyncio.gather(*list(seat.dispatcher._focus_releases))
        seat.pane_sender.assert_awaited_once()
    finally:
        _cancel_background(seat.dispatcher)


async def _turn_in_flight(_session: Any, _terminal: Any) -> TerminalActivity:
    return TerminalActivity(ComposerRead("empty"), turn_in_flight_fingerprint="run-7")


@pytest.mark.parametrize(
    ("turn_in_flight", "status", "expected", "held"),
    [
        (True, "paused", "composer_unconfirmed", False),
        (False, "active", "session_active", False),
        # Control: with neither protection in force, the same holder does hold the wake.
        (False, "paused", PANE_FOCUSED, True),
    ],
    ids=["open-turn", "active-row", "idle-control"],
)
async def test_active_turn_protection_outranks_the_focus_hold(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
    turn_in_flight: bool,
    status: str,
    expected: str,
    held: bool,
) -> None:
    seat = _seat(temp_db, session_manager, sample_project)
    composer_ledger.record_spawn(TERMINAL_ID, "host-epoch-1")
    seat.dispatcher._ism_manager = _ism_with(operator_comms_message(seat.session.id))
    seat.session.status = status
    if turn_in_flight:
        seat.dispatcher._activity_probe = _turn_in_flight
    await _focus_holder(seat.dispatcher)
    try:
        result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

        assert result.get("skipped") == expected
        seat.pane_sender.assert_not_awaited()
        held_sessions = {TERMINAL_ID: {seat.session.id: "normal"}} if held else {}
        assert seat.dispatcher._focus_holds == held_sessions
    finally:
        _cancel_background(seat.dispatcher)


@pytest.mark.parametrize(
    ("viewer", "focused", "comms", "held"),
    [
        ("gclient", True, True, True),
        ("web", True, True, True),
        ("gclient", False, True, False),
        ("web", False, True, False),
        (None, False, True, False),
        ("gclient", True, False, False),
    ],
    ids=[
        "gclient-focused",
        "web-focused",
        "gclient-unfocused",
        "web-unfocused",
        "no-registry",
        "non-comms-message",
    ],
)
async def test_focus_hold_applies_only_to_comms_wakes_with_a_focused_holder(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
    viewer: Literal["web", "gclient"] | None,
    focused: bool,
    comms: bool,
    held: bool,
) -> None:
    seat = _seat(temp_db, session_manager, sample_project)
    composer_ledger.record_spawn(TERMINAL_ID, "host-epoch-1")
    seat.session.status = "paused"
    message = operator_comms_message(seat.session.id) if comms else plain_message(seat.session.id)
    seat.dispatcher._ism_manager = _ism_with(message)
    if viewer is not None:
        await _focus_holder(seat.dispatcher, viewer=viewer, focused=focused)
    try:
        result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

        if held:
            assert result.get("skipped") == PANE_FOCUSED
            seat.pane_sender.assert_not_awaited()
            assert seat.dispatcher._focus_holds == {TERMINAL_ID: {seat.session.id: "normal"}}
        else:
            assert result.get("skipped") != PANE_FOCUSED
            assert result.get("delivered") is True
            seat.pane_sender.assert_awaited_once()
            assert seat.dispatcher._focus_holds == {}
    finally:
        _cancel_background(seat.dispatcher)


async def test_focus_release_after_the_mailbox_was_read_sends_nothing(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seat = _seat(temp_db, session_manager, sample_project)
    composer_ledger.record_spawn(TERMINAL_ID, "host-epoch-1")
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind="text", payload="leftover")
    seat.session.status = "paused"
    messages = _ism_with(operator_comms_message(seat.session.id))
    seat.dispatcher._ism_manager = messages

    async def parked_retry(delay: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(seat.dispatcher, "_composer_retry_wait", parked_retry)
    registry, attachment_id = await _focus_holder(seat.dispatcher)
    registry.set_focus_observer(seat.dispatcher.release_focus_hold)
    ledger_before = composer_ledger.read(TERMINAL_ID)
    try:
        held = await seat.dispatcher.dispatch_live_wake(seat.session.id)
        assert held.get("skipped") == PANE_FOCUSED

        # A hook read the durable message while the hold stood.
        messages.get_undelivered_messages.return_value = []
        await registry.set_focus(attachment_id, False)
        results = await asyncio.gather(*list(seat.dispatcher._focus_releases))

        assert [result.get("skipped") for result in results] == ["mailbox_read"]
        seat.pane_sender.assert_not_awaited()
        assert composer_ledger.read(TERMINAL_ID) == ledger_before
    finally:
        _cancel_background(seat.dispatcher)

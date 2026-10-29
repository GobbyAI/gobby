"""A confirmed compact can wake its pending handoff without releasing other holds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from gobby.agents.idle_detector import ComposerRead, ComposerState
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, WakeDispatcher
from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.sessions.handoff import (
    PENDING_HANDOFF_VARIABLE,
    build_handoff_continue_prompt,
    consume_pending_handoff,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer, WaitKind
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.integration


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@dataclass
class HandoffWake:
    session_id: str
    handoff_id: str
    messages: InterSessionMessageManager
    sender: AsyncMock
    batch_sender: AsyncMock
    probe: AsyncMock
    terminals: MemoryTerminalStore
    dispatcher: WakeDispatcher


@pytest.fixture
async def handoff_wake(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> AsyncIterator[HandoffWake]:
    session = session_manager.register(
        external_id="compact-wake-recipient",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
    )
    staged = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id=uuid4().hex,
        handoff=build_handoff_payload(current_state="Working", next_steps=["Continue the task"]),
        clear_session=False,
    )
    record_handoff_delivery(
        temp_db,
        handoff_id=staged.handoff_record_id,
        attempt_id=staged.attempt_id,
        boundary_kind="compact",
        continuation_session_id=session.id,
    )
    session_manager.update_status(session.id, "awaiting_handoff")
    messages = InterSessionMessageManager(temp_db)
    terminal = replace(make_memory_terminal(backend="native"), session_id=session.id)
    sender = AsyncMock()
    batch_sender = AsyncMock()
    probe = AsyncMock(return_value=TerminalActivity(composer=ComposerRead("empty", "ledger")))
    terminals = MemoryTerminalStore(terminal)
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=messages,
        tmux_sender=sender,
        native_batch_sender=batch_sender,
        terminal_manager=terminals,
        run_db=_run_db,
        activity_probe=probe,
    )
    try:
        yield HandoffWake(
            session.id,
            staged.handoff_record_id,
            messages,
            sender,
            batch_sender,
            probe,
            terminals,
            dispatcher,
        )
    finally:
        retries = list(dispatcher._composer_retries.values())
        for retry in retries:
            retry.cancel()
        await asyncio.gather(*retries, return_exceptions=True)


async def test_pending_compact_wakes_once_then_processes_queued_message(
    handoff_wake: HandoffWake, temp_db: HubDatabase, session_manager: SessionManager
) -> None:
    wake = handoff_wake
    queued = wake.messages.create_message(
        wake.session_id,
        wake.session_id,
        "Review the candidate",
        metadata_json=json.dumps({"wake_requested": True}),
    )
    replay = WakeReplayCoordinator(
        message_manager=wake.messages,
        session_manager=session_manager,
        dispatcher=wake.dispatcher,
        run_db=_run_db,
    )
    replay.bind_owner_loop(asyncio.get_running_loop())
    try:
        await replay.open()
        wake.sender.assert_awaited_once()
        assert wake.sender.await_args is not None
        assert wake.sender.await_args.args[1] == build_handoff_continue_prompt()
        assert wake.sender.await_args.kwargs["composer_confirmed_empty"] is True
        assert wake.sender.await_args.kwargs["clear_before_submit"] is False
        assert wake.messages.get_undelivered_wake_messages(wake.session_id) == [queued]

        duplicate = await wake.dispatcher.dispatch_live_wake(wake.session_id)
        assert duplicate["skipped"] == "debounced"
        wake.sender.assert_awaited_once()

        # The continuation starts a turn, whose first tool consumes get_handoff.
        session_manager.update_status(wake.session_id, "active")
        consumed = consume_pending_handoff(temp_db, wake.session_id)
        assert consumed is not None and consumed.handoff_id == wake.handoff_id
        wake.messages.mark_delivered(queued.id, wake.session_id)
        assert wake.messages.get_undelivered_wake_messages(wake.session_id) == []
        next_message = wake.messages.create_message(
            wake.session_id,
            wake.session_id,
            "Candidate approved",
            metadata_json=json.dumps({"wake_requested": True}),
        )
        session_manager.update_status(wake.session_id, "paused")
        result = await wake.dispatcher.dispatch_live_wake(wake.session_id)
        assert result["delivered"] is True
        assert wake.sender.await_args is not None
        assert wake.sender.await_args.args[1] == CONTINUE_WAKE_MESSAGE
        assert wake.messages.get_undelivered_wake_messages(wake.session_id) == [next_message]
    finally:
        await replay.close()


@pytest.mark.parametrize("status", ["interrupted", "awaiting_input", "awaiting_approval"])
async def test_compact_receipt_does_not_override_protected_session(
    handoff_wake: HandoffWake, session_manager: SessionManager, status: str
) -> None:
    wake = handoff_wake
    session_manager.update_status(wake.session_id, status)
    result = await wake.dispatcher.dispatch_live_wake(wake.session_id, priority="urgent")
    assert result["skipped"] == f"session_{status}"
    wake.sender.assert_not_awaited()


@pytest.mark.parametrize("case", ["no_receipt", "clear", "different_attempt", "consumed"])
async def test_unconfirmed_or_unrelated_handoff_cannot_wake(
    handoff_wake: HandoffWake, temp_db: HubDatabase, case: str
) -> None:
    wake = handoff_wake
    if case == "no_receipt":
        temp_db.execute(
            "DELETE FROM session_handoff_deliveries WHERE handoff_id = %s", (wake.handoff_id,)
        )
    elif case == "consumed":
        assert consume_pending_handoff(temp_db, wake.session_id) is not None
    else:
        variables = SessionVariableManager(temp_db)
        marker = variables.get_variables(wake.session_id)[PENDING_HANDOFF_VARIABLE]
        marker["clear_session" if case == "clear" else "attempt_id"] = (
            True if case == "clear" else uuid4().hex
        )
        variables.merge_variables(wake.session_id, {PENDING_HANDOFF_VARIABLE: marker})

    result = await wake.dispatcher.dispatch_live_wake(wake.session_id)
    assert result["skipped"] == "session_awaiting_handoff"
    wake.sender.assert_not_awaited()


@pytest.mark.parametrize("state", ["draft", "held", "unknown"])
async def test_continuation_wake_requires_empty_composer(
    handoff_wake: HandoffWake, state: ComposerState
) -> None:
    wake = handoff_wake
    wake.probe.return_value = TerminalActivity(composer=ComposerRead(state, "unsent text"))
    result = await wake.dispatcher.dispatch_live_wake(wake.session_id, priority="urgent")
    assert result["skipped"] == (
        "composer_occupied" if state == "draft" else "composer_unconfirmed"
    )
    wake.sender.assert_not_awaited()


@pytest.mark.parametrize("race", ["interrupted", "awaiting_input", "awaiting_approval", "consumed"])
async def test_continuation_rechecks_handoff_and_holds_after_composer_probe(
    handoff_wake: HandoffWake, temp_db: HubDatabase, session_manager: SessionManager, race: str
) -> None:
    wake = handoff_wake

    async def changed_state(_session: object, _terminal: object | None) -> TerminalActivity:
        if race == "consumed":
            assert consume_pending_handoff(temp_db, wake.session_id) is not None
        else:
            session_manager.update_status(wake.session_id, race)
        return TerminalActivity(composer=ComposerRead("empty", "ledger"))

    wake.probe.side_effect = changed_state
    result = await wake.dispatcher.dispatch_live_wake(wake.session_id)
    assert result["error_code"] == "handoff_continuation_withheld"
    wake.sender.assert_not_awaited()


@pytest.mark.parametrize("kind", ["input", "approval"])
async def test_open_wait_with_awaiting_handoff_status_stays_protected(
    handoff_wake: HandoffWake, session_manager: SessionManager, kind: WaitKind
) -> None:
    wake = handoff_wake
    lifecycle = TurnLifecycleReducer(session_manager)
    lifecycle.begin_turn(wake.session_id, evidence=TurnEvidence(source="hook"))
    lifecycle.enter_wait(
        wake.session_id, kind=kind, token="human-question", evidence=TurnEvidence(source="hook")
    )
    session_manager.update_status(wake.session_id, "awaiting_handoff")

    result = await wake.dispatcher.dispatch_live_wake(wake.session_id)

    assert result["error_code"] == "handoff_continuation_withheld"
    assert f"open {kind} wait" in result["error_message"]
    wake.sender.assert_not_awaited()


async def test_open_turn_with_empty_composer_cannot_receive_continuation(
    handoff_wake: HandoffWake,
) -> None:
    wake = handoff_wake
    wake.probe.return_value = TerminalActivity(
        composer=ComposerRead("empty", "ledger"), turn_in_flight_fingerprint="new-turn"
    )
    result = await wake.dispatcher.dispatch_live_wake(wake.session_id)
    assert result["skipped"] == "composer_unconfirmed"
    wake.sender.assert_not_awaited()


async def test_native_broadcast_recovers_handoff_alongside_ordinary_recipient(
    handoff_wake: HandoffWake, session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    wake = handoff_wake
    ordinary = session_manager.register(
        external_id="ordinary-broadcast-recipient",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
    )
    session_manager.update_status(ordinary.id, "paused")
    terminal = replace(make_memory_terminal(backend="native"), session_id=ordinary.id)
    wake.terminals.rows[terminal.id] = terminal
    wake.batch_sender.return_value = [
        {"session_id": ordinary.id, "delivered": True, "method": "terminal"}
    ]

    results = await wake.dispatcher.dispatch_live_wakes([wake.session_id, ordinary.id])

    assert [result["delivered"] for result in results] == [True, True]
    wake.sender.assert_awaited_once()
    assert wake.sender.await_args is not None
    assert wake.sender.await_args.args[1] == build_handoff_continue_prompt()
    wake.batch_sender.assert_awaited_once()
    assert wake.batch_sender.await_args is not None
    assert [target.session_id for target in wake.batch_sender.await_args.args[0]] == [ordinary.id]

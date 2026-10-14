"""A wake withheld by a host gap or an interrupt names the seat and its release valve."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.live_wake import ActivityProbe
from gobby.events.wake import WakeDispatcher
from gobby.runner_init.wake_activity import probe_terminal_activity
from gobby.storage.attention import AttentionStateManager, session_attention_entry_id
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead, WriteKind
from tests.events.test_wake import FakeSession
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

TERMINAL_ID = "terminal-1"
HOST_EPOCH = "host-epoch-1"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@dataclass
class SeatSession(FakeSession):
    ref: str = ""


@dataclass
class Seat:
    session: SeatSession
    dispatcher: WakeDispatcher
    attention: AttentionStateManager
    notifications: list[dict[str, object]]
    pane_sender: AsyncMock

    @property
    def entry_id(self) -> str:
        return session_attention_entry_id(self.session.id)


def _seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    *,
    agent_depth: int = 0,
) -> Seat:
    with pytest.MonkeyPatch.context() as identity:
        patch_local_machine_id(identity, MACHINE_ID)
        row = session_manager.register(
            external_id=f"gap-seat-{agent_depth}",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
    session = SeatSession(
        id=row.id, ref=row.ref, agent_depth=agent_depth, terminal_context={"tmux_pane": "%7"}
    )
    sessions = MagicMock()
    sessions.get.return_value = session
    terminals = MagicMock()
    terminals.resolve_live_for_session.return_value = MagicMock(id=TERMINAL_ID)
    notifications: list[dict[str, object]] = []
    attention = AttentionStateManager(
        temp_db, notification_publisher=notifications.append, epoch="test-epoch"
    )
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=sessions,
        ism_manager=MagicMock(),
        tmux_sender=pane_sender,
        terminal_manager=terminals,
        activity_probe=cast(ActivityProbe, probe_terminal_activity),
        attention_manager=attention,
    )
    return Seat(session, dispatcher, attention, notifications, pane_sender)


def _cancel_retries(dispatcher: WakeDispatcher) -> None:
    for task in dispatcher._composer_retries.values():
        task.cancel()


@pytest.fixture
def seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> Iterator[Seat]:
    built = _seat(temp_db, session_manager, sample_project)
    yield built
    _cancel_retries(built.dispatcher)


@pytest.fixture(autouse=True)
def _park_composer_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries park on their first wait; each test dispatches the attempts it needs."""

    async def park(_self: WakeDispatcher, _delay: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(WakeDispatcher, "_composer_retry_wait", park)


@pytest.mark.asyncio
async def test_ring_overflow_resume_marks_the_gap_and_names_the_seat_and_valve(
    seat: Seat, composer_ledger: ComposerLedger
) -> None:
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    # The daemon was down while the host ring evicted input past the reader's cursor,
    # so the reader resubscribes with stream.gap.
    composer_ledger.resume_host(HOST_EPOCH, 300, since=0, gap=True)
    assert composer_ledger.read(TERMINAL_ID) == LedgerRead("blocked", "gap")

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["skipped"] == "composer_unconfirmed"
    seat.pane_sender.assert_not_awaited()
    item = seat.attention.get(seat.entry_id)
    assert item is not None
    assert item.state == "blocked"
    assert item.session_id == seat.session.id
    assert item.reason == "composer_blocked"
    assert item.kind == "actionable"
    assert item.fingerprint == "composer_blocked:gap"
    message = str(item.payload["message"])
    assert message.startswith("Check pane has no draft; run release_composer(")
    assert seat.session.ref in message
    assert "release_composer" in message
    assert len(seat.notifications) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["gap", "interrupt"])
async def test_repeated_withheld_wakes_keep_one_attention_item(
    seat: Seat, composer_ledger: ComposerLedger, block: str
) -> None:
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    if block == "gap":
        composer_ledger.resume_host(HOST_EPOCH, 300, since=0, gap=True)
    else:
        composer_ledger.block(TERMINAL_ID, "interrupt")

    await seat.dispatcher.dispatch_live_wake(seat.session.id)
    first = seat.attention.get(seat.entry_id)
    for _ in range(3):
        await seat.dispatcher.dispatch_live_wake(seat.session.id)

    current = seat.attention.get(seat.entry_id)
    assert first is not None and current is not None
    assert current.fingerprint == f"composer_blocked:{block}"
    assert current.attention_id == first.attention_id
    assert len(seat.notifications) == 1
    seat.pane_sender.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "\x1b[I",
        "\x1b[O",
        "\x1b[<0;12;8M",
        "\x1b[<64;12;8M",
        "\x1b[M !!",
        "\x1b[M`!!",
        "\x1b[A",
        "\x1bOD",
    ],
)
async def test_non_text_input_allows_following_wake(
    seat: Seat, composer_ledger: ComposerLedger, payload: str
) -> None:
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind="input", payload=payload)
    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)
    assert result["delivered"] is True
    seat.pane_sender.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["control", "cursor", "edit"])
async def test_native_input_effect_controls_following_wake(
    seat: Seat, composer_ledger: ComposerLedger, effect: str
) -> None:
    from gobby.terminals.host_events import decode_host_event

    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.observe_host_event(
        decode_host_event(
            {
                "event": "input_activity",
                "terminal_id": TERMINAL_ID,
                "host_terminal_id": "ht-1",
                "attachment_id": "att-1",
                "kind": "input",
                "bytes": 3,
                "interrupt": None,
                "submit": False,
                "effect": effect,
                "epoch": HOST_EPOCH,
                "seq": 1,
            }
        )
    )
    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)
    assert result["delivered"] is (effect != "edit")
    if effect == "edit":
        assert result["decline_reason"] == "composer_occupied"
        seat.pane_sender.assert_not_awaited()
    else:
        seat.pane_sender.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload"),
    [("input", "typed"), ("input", "\x1b[200~paste\x1b[201~"), ("paste", "paste")],
)
async def test_text_input_still_withholds_wake(
    seat: Seat, composer_ledger: ComposerLedger, kind: WriteKind, payload: str
) -> None:
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind=kind, payload=payload)
    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)
    assert result["delivered"] is False
    assert result["skipped"] == "composer_occupied"
    seat.pane_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_released_composer_delivers_the_wake_and_retires_the_item(
    seat: Seat, composer_ledger: ComposerLedger
) -> None:
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.resume_host(HOST_EPOCH, 300, since=0, gap=True)
    await seat.dispatcher.dispatch_live_wake(seat.session.id)
    assert seat.attention.get(seat.entry_id) is not None

    composer_ledger.release(TERMINAL_ID)
    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is True
    seat.pane_sender.assert_awaited_once()
    cleared = seat.attention.get(seat.entry_id)
    assert cleared is not None
    assert cleared.state is None
    assert cleared.reason is None


@pytest.mark.asyncio
async def test_arrow_over_held_daemon_text_withholds_wake(
    seat: Seat, composer_ledger: ComposerLedger
) -> None:
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.observe_write(TERMINAL_ID, origin="daemon", kind="text", payload="continue")
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind="input", payload="\x1b[A")
    assert composer_ledger.read(TERMINAL_ID) == LedgerRead("draft")
    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)
    assert result["skipped"] == "composer_occupied"
    seat.pane_sender.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["provider_limit", "untracked"])
async def test_blocks_with_their_own_valve_raise_no_composer_item(
    seat: Seat, composer_ledger: ComposerLedger, block: str
) -> None:
    # A usage limit raises its own provider item; an untracked seat (a tmux pane, a
    # seat bound before the ledger) has always read unknown until release_composer.
    if block == "provider_limit":
        composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
        composer_ledger.block(TERMINAL_ID, "provider_limit")

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["skipped"] == "composer_unconfirmed"
    assert seat.attention.get(seat.entry_id) is None
    assert seat.notifications == []


@pytest.mark.asyncio
async def test_a_seat_already_flagged_for_another_reason_keeps_that_item(
    seat: Seat, composer_ledger: ComposerLedger
) -> None:
    flagged = seat.attention.transition(
        seat.entry_id,
        state="blocked",
        session_id=seat.session.id,
        reason="provider_error",
        kind="non_actionable",
        fingerprint="provider_error:1:3",
        payload={"message": "usage limit"},
    )
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.block(TERMINAL_ID, "interrupt")

    await seat.dispatcher.dispatch_live_wake(seat.session.id)

    current = seat.attention.get(seat.entry_id)
    assert flagged.current is not None and current is not None
    assert current.attention_id == flagged.current.attention_id
    assert current.reason == "provider_error"
    assert len(seat.notifications) == 1


@pytest.mark.asyncio
async def test_spawned_agent_wakes_raise_no_seat_item(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
) -> None:
    # The interactive monitor sweeps session items of non-interactive sessions every
    # pass, so an agent item would reopen (and notify) on each retry.
    agent = _seat(temp_db, session_manager, sample_project, agent_depth=1)
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    composer_ledger.resume_host(HOST_EPOCH, 300, since=0, gap=True)

    try:
        result = await agent.dispatcher.dispatch_live_wake(agent.session.id)
    finally:
        _cancel_retries(agent.dispatcher)

    assert result["skipped"] == "composer_unconfirmed"
    assert agent.attention.get(agent.entry_id) is None

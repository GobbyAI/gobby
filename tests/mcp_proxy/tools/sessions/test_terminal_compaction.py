"""Grok compact handoff delivery: Ctrl+C interrupt, one retry after a rejection.

Grok 1.0.30 never cancels a turn on Esc; Ctrl+C on an empty composer cancels it,
and its shell rejects ``/compact`` while a turn ("task") is still running. The
sender must interrupt with Ctrl+C, detect the rejection, interrupt again, and
resubmit exactly once before it reports the delivery as failed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.agents.idle_detector import ComposerRead, ComposerState
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _COMPACTION_REJECTION_ERROR_CODE,
    _COMPOSER_OCCUPIED_ERROR_CODE,
    _INTERRUPT_ATTEMPTS,
    _INTERRUPT_UNCONFIRMED_ERROR_CODE,
    _OBSERVED_INTERRUPT_SETTLE_SECONDS,
    _TURN_SETTLE_POLL_SECONDS,
    _confirm_interrupt,
    _send_terminal_compaction_command,
)
from gobby.sessions.transcript_cursor import (
    build_interrupt_observer,
    build_turn_settled_observer,
)
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.runtime import SnapshotMode

pytestmark = pytest.mark.unit

_SETTLE = 0.02
_COMMAND = "/compact"
_REJECTION = f"'{_COMMAND}' is disabled while a task is in progress"
_REJECTED_SCREEN = f"\n{_REJECTION}\n> "
_DRAIN = composer_clear_sequence("grok")


class _GrokPane:
    """PaneIO fake: each Enter appends the next scripted pane output, if any."""

    backend = "native"
    target = "term-grok"

    def __init__(self, outputs_after_enter: list[str] | None = None) -> None:
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.screen = "output\n> "
        self.snapshot_modes: list[SnapshotMode] = []
        self._outputs = list(outputs_after_enter or [])

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        # The write carries its own newline, so the command submits here and the
        # CLI's answer to it lands on the next frame.
        if self._outputs:
            self.screen += self._outputs.pop(0)
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        self.snapshot_modes.append(mode)
        return self.screen


async def _send(
    pane: _GrokPane,
    observe: Callable[[], bool | None] | None,
    *,
    turn_settled: Callable[[], bool | None] | None = None,
) -> tuple[tuple[bool, str | None, bool, dict[str, object] | None], MagicMock, MagicMock]:
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    result = await _send_terminal_compaction_command(
        pane,
        _COMMAND,
        "session-grok",
        cli_source="grok",
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=observe,
        turn_settled=turn_settled,
        settle_seconds=_SETTLE,
    )
    return result, mark, clear


@pytest.mark.asyncio
@pytest.mark.parametrize("observe", [None, lambda: True], ids=["blind", "observed"])
async def test_grok_compaction_interrupt_uses_ctrl_c(
    observe: Callable[[], bool | None] | None,
) -> None:
    pane = _GrokPane()

    result, mark, clear = await _send(pane, observe)

    assert result == (True, None, True, None)
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter"]
    assert "escape" not in pane.keys
    assert pane.typed == [f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("observe", [None, lambda: True], ids=["blind", "observed"])
async def test_grok_compaction_retries_after_interrupt(
    observe: Callable[[], bool | None] | None,
) -> None:
    pane = _GrokPane([_REJECTED_SCREEN])

    result, mark, clear = await _send(pane, observe)

    assert result == (True, None, True, None)
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter", "ctrl_c", *_DRAIN, "enter"]
    assert pane.typed == [f"{_COMMAND}\n", f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_grok_retry_resubmits_only_after_the_interrupt_is_confirmed() -> None:
    pane = _GrokPane([_REJECTED_SCREEN])
    typed_at_confirmation: list[int] = []

    def observe() -> bool:
        typed_at_confirmation.append(len(pane.typed))
        return True

    result, _mark, _clear = await _send(pane, observe)

    assert result == (True, None, True, None)
    assert pane.typed == [f"{_COMMAND}\n", f"{_COMMAND}\n"]
    # The first confirmation precedes both submissions; the second precedes the retry.
    assert typed_at_confirmation == [0, 1]


@pytest.mark.asyncio
async def test_grok_retry_types_nothing_when_the_second_interrupt_is_unconfirmed() -> None:
    pane = _GrokPane([_REJECTED_SCREEN])
    confirmations = iter([True])

    result, mark, clear = await _send(pane, lambda: next(confirmations, False))

    ok, reason, pending, detail = result
    assert ok is False
    assert reason == f"CLI did not confirm interruption after {_INTERRUPT_ATTEMPTS} attempts"
    assert pending is False
    assert detail == {
        "error_code": _INTERRUPT_UNCONFIRMED_ERROR_CODE,
        "continuation_pending": False,
    }
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter", *(["ctrl_c"] * _INTERRUPT_ATTEMPTS)]
    assert pane.typed == [f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_grok_compaction_rejected_twice_fails_the_delivery() -> None:
    pane = _GrokPane([_REJECTED_SCREEN, _REJECTED_SCREEN])

    result, mark, clear = await _send(pane, lambda: True)

    assert result == (
        False,
        _REJECTION,
        False,
        {
            "error_code": _COMPACTION_REJECTION_ERROR_CODE,
            "rejected_command": _COMMAND,
            "rejection_message": _REJECTION,
        },
    )
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter", "ctrl_c", *_DRAIN, "enter"]
    assert pane.typed == [f"{_COMMAND}\n", f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_called_once()


# A settled turn (the CLI's own transcript shows its last turn ended) must never be
# interrupted: Grok's Ctrl+C on an idle composer escalates toward quit and Codex's
# quits outright. The command is submitted directly and the result says so.
@pytest.mark.asyncio
async def test_grok_settled_turn_is_compacted_without_an_interrupt() -> None:
    pane = _GrokPane()

    result, mark, clear = await _send(pane, lambda: True, turn_settled=lambda: True)

    assert result == (True, None, True, {"interrupted": False})
    assert pane.keys == [*_DRAIN, "enter"]
    assert "ctrl_c" not in pane.keys
    assert pane.typed == [f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


def _grok_event(kind: str, **fields: object) -> bytes:
    return (json.dumps({"type": kind, **fields}) + "\n").encode()


def _recorded_compact_events() -> list[dict[str, object]]:
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures/provider_contracts/grok/goal_compact_2026_09_23.jsonl"
    )
    return [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]


class _CountingGrokPane(_GrokPane):
    """Counts Ctrl+C presses while still recording every key."""

    def __init__(self) -> None:
        super().__init__()
        self.ctrl_c_presses = 0

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        if key == "ctrl_c":
            self.ctrl_c_presses += 1
        return await super().send_key(key)


@pytest.mark.asyncio
async def test_grok_goal_mode_gap_interrupts_the_successor_without_the_settle_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """turn_ended then turn_started is the settle point. The successor turn is interrupted.

    Goal mode starts the next turn about 92ms after turn_ended. The settle poll is
    0.25s, so the idle gap is gone before the next read. Waiting out the 30s budget
    is the bug.
    """
    updates = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    updates.write_bytes(b"")
    events.write_bytes(_grok_event("turn_started", turn_number=18))
    probe = build_turn_settled_observer("grok", updates, session_id="session-grok")
    assert probe is not None
    events.write_bytes(
        events.read_bytes()
        + _grok_event("turn_ended", outcome="completed")
        + _grok_event("turn_started", turn_number=19)
    )

    clock = {"now": 0.0}
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal_compaction.time.monotonic",
        monotonic,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal_compaction.asyncio.sleep",
        sleep,
    )

    pane = _GrokPane()
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    result = await _send_terminal_compaction_command(
        pane,
        _COMMAND,
        "session-grok",
        cli_source="grok",
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=lambda: True,
        turn_settled=probe,
        settle_seconds=None,
    )

    assert _TURN_SETTLE_POLL_SECONDS not in sleeps
    assert clock["now"] < 10
    assert result[0] is True
    assert pane.keys[0] == "ctrl_c"


@pytest.mark.asyncio
async def test_grok_interrupt_stops_pressing_ctrl_c_once_the_turn_has_ended() -> None:
    """A turn that has ended, with no newer turn_started, gets no further Ctrl+C."""
    pane = _CountingGrokPane()

    result, _mark, _clear = await _send(
        pane,
        lambda: False,
        turn_settled=lambda: pane.ctrl_c_presses > 0,
    )

    assert pane.ctrl_c_presses == 1
    assert result[0] is True
    assert pane.typed == [f"{_COMMAND}\n"]


@pytest.mark.asyncio
async def test_grok_completed_turn_then_goal_successor_is_not_interrupt_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settled = False
    cancelled = False
    presses = 0

    class _GoalPane(_CountingGrokPane):
        async def send_key(self, key: str) -> tuple[bool, str | None]:
            nonlocal settled, cancelled, presses
            if key == "ctrl_c":
                presses += 1
                if presses == 1:
                    settled = True  # completed; goal mode may restart
                else:
                    cancelled = True
                    settled = True
            return await super().send_key(key)

    async def settle_goal_mode(_seconds: float) -> None:
        nonlocal settled
        if presses == 1:
            settled = False  # successor turn_started 92 ms later

    monkeypatch.setattr(asyncio, "sleep", settle_goal_mode)
    pane = _GoalPane()
    result = await _confirm_interrupt(
        pane,
        "ctrl_c",
        "session-grok",
        lambda: cancelled,
        attempt_seconds=0,
        turn_settled=lambda: settled,
    )

    assert result == (True, None, None)
    assert presses == 2


@pytest.mark.asyncio
async def test_grok_interrupt_confirms_when_ctrl_c_restarts_the_loop_then_cancels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The first Ctrl+C restarts Grok's model loop. The next one cancels the turn."""
    updates = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    updates.write_bytes(b"")
    events.write_bytes(_grok_event("turn_started", turn_number=20))
    interrupt = build_interrupt_observer("grok", updates, session_id="session-grok")
    probe = build_turn_settled_observer("grok", updates, session_id="session-grok")
    assert interrupt is not None
    assert probe is not None
    with events.open("ab") as stream:
        stream.write(_grok_event("turn_ended", outcome="completed"))
        stream.write(_grok_event("turn_started", turn_number=21))
        stream.write(_grok_event("loop_started", loop_index=0))

    class _RestartPane(_GrokPane):
        def __init__(self) -> None:
            super().__init__()
            self.ctrl_c_presses = 0

        async def send_key(self, key: str) -> tuple[bool, str | None]:
            if key == "ctrl_c":
                self.ctrl_c_presses += 1
                if self.ctrl_c_presses == 1:
                    payload = _grok_event("loop_started", loop_index=0)
                else:
                    payload = _grok_event(
                        "turn_ended",
                        outcome="cancelled",
                        cancellation_category="mid_turn_abort",
                    )
                with events.open("ab") as stream:
                    stream.write(payload)
            return await super().send_key(key)

    pane = _RestartPane()
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    clock = {"now": 0.0}

    async def sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal_compaction.time.monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal_compaction.asyncio.sleep",
        sleep,
    )
    result = await _send_terminal_compaction_command(
        pane,
        _COMMAND,
        "session-grok",
        cli_source="grok",
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=interrupt,
        turn_settled=probe,
        interrupt_settle_seconds=_OBSERVED_INTERRUPT_SETTLE_SECONDS,
        settle_seconds=None,
    )

    assert clock["now"] < 30  # No 30-second pre-interrupt settle wait.
    assert result[0] is True
    assert pane.ctrl_c_presses == 2
    assert pane.typed == [f"{_COMMAND}\n"]


@pytest.mark.asyncio
async def test_grok_recorded_goal_mode_compact_replay_succeeds_on_first_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Replay the 14:21 92 ms gap and loop restart, then the 14:23 cancellation."""
    recorded = _recorded_compact_events()
    assert recorded[0]["ts"] == "2026-09-23T19:21:14.997Z"
    assert recorded[1]["ts"] == "2026-09-23T19:21:15.089Z"
    updates = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    updates.write_bytes(b"")
    events.write_bytes(_grok_event("turn_started", turn_number=18))
    interrupt = build_interrupt_observer("grok", updates, session_id="session-grok")
    probe = build_turn_settled_observer("grok", updates, session_id="session-grok")
    assert interrupt is not None
    assert probe is not None
    with events.open("ab") as stream:
        for record in recorded[:3]:
            stream.write((json.dumps(record) + "\n").encode())

    clock = {"now": 0.0}
    restart_at = 9.0  # About 9 seconds after the failed attempt's last Ctrl+C.
    cancel_after_second_press = 3.826  # Recorded loop restart to cancelled turn.
    restart_recorded = False
    cancel_recorded = False
    cancel_at: float | None = None

    async def sleep(seconds: float) -> None:
        nonlocal restart_recorded, cancel_recorded
        clock["now"] += seconds
        if clock["now"] >= restart_at and not restart_recorded:
            with events.open("ab") as stream:
                stream.write((json.dumps(recorded[3]) + "\n").encode())
            restart_recorded = True
        if cancel_at is not None and clock["now"] >= cancel_at and not cancel_recorded:
            with events.open("ab") as stream:
                stream.write((json.dumps(recorded[-1]) + "\n").encode())
            cancel_recorded = True

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal_compaction.time.monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.sessions._terminal_compaction.asyncio.sleep",
        sleep,
    )

    class _ReplayPane(_CountingGrokPane):
        async def send_key(self, key: str) -> tuple[bool, str | None]:
            nonlocal cancel_at
            if key == "ctrl_c" and self.ctrl_c_presses and restart_recorded:
                cancel_at = clock["now"] + cancel_after_second_press
            return await super().send_key(key)

    pane = _ReplayPane()
    result = await _send_terminal_compaction_command(
        pane,
        _COMMAND,
        "session-grok",
        cli_source="grok",
        mark_continuation_pending=lambda: True,
        clear_continuation_pending=lambda: True,
        observe_interrupt=interrupt,
        turn_settled=probe,
        interrupt_settle_seconds=_OBSERVED_INTERRUPT_SETTLE_SECONDS,
        settle_seconds=None,
    )

    assert restart_recorded is True
    assert cancel_recorded is True
    assert clock["now"] < 20
    assert result[0] is True
    assert pane.ctrl_c_presses == 2
    assert pane.typed == [f"{_COMMAND}\n"]


@pytest.mark.asyncio
async def test_compaction_waits_for_a_live_turn_to_settle_before_submitting() -> None:
    pane = _GrokPane()
    polls = {"n": 0}

    def turn_settled() -> bool:
        polls["n"] += 1
        return polls["n"] >= 2

    result, mark, clear = await _send(pane, lambda: True, turn_settled=turn_settled)

    assert polls["n"] >= 2
    assert result == (True, None, True, {"interrupted": False})
    assert pane.keys == [*_DRAIN, "enter"]
    assert "ctrl_c" not in pane.keys
    assert pane.typed == [f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_compaction_interrupts_after_the_turn_settle_wait_times_out() -> None:
    pane = _GrokPane()
    polls = {"n": 0}

    def turn_settled() -> bool:
        polls["n"] += 1
        return False

    result, mark, clear = await _send(pane, lambda: True, turn_settled=turn_settled)

    assert polls["n"] >= 2
    assert result == (True, None, True, None)
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter"]
    assert pane.typed == [f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", [False, None], ids=["live", "unknown"])
async def test_grok_live_or_unknown_turn_is_interrupted_before_compaction(
    settled: bool | None,
) -> None:
    pane = _GrokPane()

    result, mark, clear = await _send(pane, lambda: True, turn_settled=lambda: settled)

    assert result == (True, None, True, None)
    assert pane.keys == ["ctrl_c", *_DRAIN, "enter"]
    assert pane.typed == [f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_grok_rejection_after_a_settled_submission_interrupts_before_resubmitting() -> None:
    # The rejection proves a turn is running after all: interrupt it, then resubmit once.
    pane = _GrokPane([_REJECTED_SCREEN])
    reads = iter([True, True, False, False])

    result, mark, clear = await _send(pane, lambda: True, turn_settled=lambda: next(reads, False))

    assert result == (True, None, True, None)
    assert pane.keys == [*_DRAIN, "enter", "ctrl_c", *_DRAIN, "enter"]
    assert pane.typed == [f"{_COMMAND}\n", f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_grok_rejection_resubmission_checks_idle_before_first_ctrl_c() -> None:
    pane = _GrokPane([_REJECTED_SCREEN])

    result, mark, clear = await _send(pane, lambda: True, turn_settled=lambda: True)

    assert "ctrl_c" not in pane.keys
    assert result == (True, None, True, {"interrupted": False})
    assert pane.typed == [f"{_COMMAND}\n", f"{_COMMAND}\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_compaction_refuses_occupied_composer_before_interrupt() -> None:
    pane = _GrokPane()
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)

    ok, reason, pending, detail = await _send_terminal_compaction_command(
        pane,
        _COMMAND,
        "session-grok",
        cli_source="grok",
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=lambda: True,
        settle_seconds=_SETTLE,
        composer_read=lambda _text: ComposerRead("draft", "hello draft"),
    )

    assert (ok, pending) == (False, False)
    assert reason == "composer holds an operator draft"
    assert detail == {"error_code": _COMPOSER_OCCUPIED_ERROR_CODE, "continuation_pending": False}
    assert pane.keys == [] and pane.typed == []
    # Faint suggestion and placeholder text is only distinguishable with styling.
    assert pane.snapshot_modes == ["ansi"]
    mark.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["empty", "unknown"])
async def test_non_draft_reads_after_enter_compact(state: ComposerState) -> None:
    """An empty composer after the Enter proves the command went in. A frame nobody
    can read is not a failure either: the write and the Enter were delivered, and
    retyping into a composer that may have taken them would compact twice."""
    pane = _GrokPane()
    mark = MagicMock(return_value=True)
    ok, _reason, _pending, _detail = await _send_terminal_compaction_command(
        pane,
        _COMMAND,
        "session-grok",
        cli_source="grok",
        mark_continuation_pending=mark,
        clear_continuation_pending=MagicMock(return_value=True),
        settle_seconds=_SETTLE,
        composer_read=lambda _text: ComposerRead(state),
    )
    assert ok is True
    assert pane.typed == [f"{_COMMAND}\n"]

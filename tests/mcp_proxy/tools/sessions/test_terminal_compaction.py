"""Grok compact handoff delivery: Ctrl+C interrupt, one retry after a rejection.

Grok 1.0.30 never cancels a turn on Esc; Ctrl+C on an empty composer cancels it,
and its shell rejects ``/compact`` while a turn ("task") is still running. The
sender must interrupt with Ctrl+C, detect the rejection, interrupt again, and
resubmit exactly once before it reports the delivery as failed.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from gobby.agents.idle_detector import ComposerRead, ComposerState
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _COMPACTION_REJECTION_ERROR_CODE,
    _COMPOSER_OCCUPIED_ERROR_CODE,
    _INTERRUPT_ATTEMPTS,
    _INTERRUPT_UNCONFIRMED_ERROR_CODE,
    _send_terminal_compaction_command,
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
        if key == "enter" and self._outputs:
            self.screen += self._outputs.pop(0)
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
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
    assert pane.typed == [_COMMAND]
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
    assert pane.typed == [_COMMAND, _COMMAND]
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
    assert pane.typed == [_COMMAND, _COMMAND]
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
    assert pane.typed == [_COMMAND]
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
    assert pane.typed == [_COMMAND, _COMMAND]
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
    assert pane.typed == [_COMMAND]
    mark.assert_called_once()
    clear.assert_not_called()


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
    assert pane.typed == [_COMMAND]
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
    assert pane.typed == [_COMMAND]
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
    assert pane.typed == [_COMMAND]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_grok_rejection_after_a_settled_submission_interrupts_before_resubmitting() -> None:
    # The rejection proves a turn is running after all: interrupt it, then resubmit once.
    pane = _GrokPane([_REJECTED_SCREEN])

    result, mark, clear = await _send(pane, lambda: True, turn_settled=lambda: True)

    assert result == (True, None, True, None)
    assert pane.keys == [*_DRAIN, "enter", "ctrl_c", *_DRAIN, "enter"]
    assert pane.typed == [_COMMAND, _COMMAND]
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
async def test_non_draft_reads_compact_as_before(state: ComposerState) -> None:
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
    assert pane.typed == [_COMMAND]

"""Verified handoff command delivery: confirm the interrupt, clear, verify, then Enter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.mcp_proxy.tools.sessions import _terminal
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _COMMAND_NOT_SUBMITTED_ERROR_CODE,
    _INTERRUPT_ATTEMPTS,
    _INTERRUPT_UNCONFIRMED_ERROR_CODE,
    NO_TERMINAL_TARGET_ERROR_CODE,
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    _wait_for_compact_boundary,
    deliver_staged_compact_handoff,
)
from gobby.sessions.compact_continuation import CompactBoundaryWaiter
from gobby.sessions.transcript_cursor import CodexRolloutCursor
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.pane_io import ComposerReader, RuntimePaneIO
from gobby.terminals.runtime import SnapshotMode
from tests.agents.detection_test_support import BundledDetectionRegistry

pytestmark = pytest.mark.unit

_SETTLE = 0.02
_CLAUDE_READ = IdleDetector(BundledDetectionRegistry(), "claude").composer_read
_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"
_COMPACTION = "gobby.mcp_proxy.tools.sessions._terminal_compaction"
_RULE = "─" * 40


def _claude_frame(composer: str) -> str:
    """One Claude screen whose composer row holds ``composer``."""
    return "\n".join(["output", _RULE, f"❯ {composer}".rstrip(), _RULE, "  auto mode on"])


class _ComposerPane:
    """PaneIO fake that records keys and typed text."""

    backend = "native"
    target = "term-1"

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.typed: list[str] = []

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        return "output\n> "


class _ConfirmModalPane(_ComposerPane):
    """Pane that redraws Droid's confirm modal over the screen once a command is submitted.

    The command submits on its own write, so the modal follows the typed text
    rather than a separate Enter key.
    """

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        if self.typed:
            return "Confirm /compress\nEnter to confirm, ESC to cancel"
        return "output\n> "


class _UnsubmittedPane(_ComposerPane):
    """Claude pane that keeps the typed command, as a busy composer does.

    The composer empties once ``recovers_after`` recovery Enters have been sent, so
    zero models a CLI that took the write's own newline and one models a CLI that
    held it behind a paste review gate; ``None`` never submits.
    """

    def __init__(self, recovers_after: int | None = None) -> None:
        super().__init__()
        self.recovers_after = recovers_after

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        enters = self.keys.count("enter")
        if not self.typed or (self.recovers_after is not None and enters >= self.recovers_after):
            return _claude_frame("")
        return _claude_frame(self.typed[-1])


class _RetypeOnlyPane(_ComposerPane):
    """Claude pane whose Enters are literal newlines until the command is retyped.

    Models the other half of the ladder: more Enters never help, and only a drained
    and retyped command submits.
    """

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        if not self.typed or len(self.typed) > 1:
            return _claude_frame("")
        return _claude_frame(self.typed[-1])


async def _send(
    pane: _ComposerPane,
    observe: Callable[[], bool | None],
    *,
    cli_source: str = "claude",
    command: str = "/clear",
    composer_read: ComposerReader | None = None,
) -> tuple[tuple[bool, str | None, bool, dict[str, object] | None], MagicMock, MagicMock]:
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    result = await _send_terminal_compaction_command(
        pane,
        command,
        "session-1",
        cli_source=cli_source,
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=observe,
        settle_seconds=_SETTLE,
        composer_read=composer_read,
    )
    return result, mark, clear


@pytest.mark.asyncio
async def test_confirmed_interrupt_drains_then_submits_once() -> None:
    pane = _ComposerPane()

    result, mark, clear = await _send(pane, lambda: True)

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", *composer_clear_sequence("claude"), "enter"]
    assert pane.typed == ["/clear\n"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_codex_uses_escape_and_the_line_drain() -> None:
    pane = _ComposerPane()

    result, _mark, _clear = await _send(pane, lambda: True, cli_source="codex")

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", *composer_clear_sequence("codex"), "enter"]
    assert pane.typed == ["/clear\n"]


@pytest.mark.asyncio
async def test_failed_followup_enter_keeps_codex_compact_attempt_pending() -> None:
    class FailedEnterPane(_ComposerPane):
        async def send_key(self, key: str) -> tuple[bool, str | None]:
            self.keys.append(key)
            return (False, "native key write failed") if key == "enter" else (True, None)

    pane = FailedEnterPane()
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    result = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        schedule_continuation_readiness=schedule,
        continuation_readiness_capture_lines=100,
        turn_settled=lambda: True,
        settle_seconds=0,
    )

    assert result == (True, None, True, {"enter_delivery_unconfirmed": True})
    assert pane.typed == ["/compact\n"]
    assert pane.keys == [*composer_clear_sequence("codex"), "enter"]
    mark.assert_called_once()
    clear.assert_not_called()
    schedule.assert_called_once()


@pytest.mark.asyncio
async def test_codex_rollout_compact_completes_wait_without_postcompact_hook(
    tmp_path: Path,
) -> None:
    import asyncio

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text('{"type":"session_meta","payload":{"id":"codex-session"}}\n')
    cursor = CodexRolloutCursor.at_eof(rollout)
    with rollout.open("a") as stream:
        stream.write('{"type":"compacted","timestamp":"2026-09-27T16:00:20Z"}\n')
    waiter = CompactBoundaryWaiter(
        "attempt", "handoff", None, asyncio.Event(), asyncio.get_running_loop()
    )
    calls: list[str] = []

    def receive_boundary() -> bool:
        calls.append("compacted")
        waiter.loop.call_soon_threadsafe(waiter.event.set)
        return True

    result = await _wait_for_compact_boundary(
        waiter,
        _ComposerPane(),
        None,
        None,
        timeout_seconds=0.1,
        codex_cursor=cursor,
        on_codex_boundary=receive_boundary,
    )

    assert result is None
    assert calls == ["compacted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "enters"), [("/compress", 1), ("/clear", 0)])
async def test_droid_presses_enter_on_the_compress_confirm_modal_only(
    command: str, enters: int
) -> None:
    pane = _ConfirmModalPane()

    result, _mark, _clear = await _send(pane, lambda: True, cli_source="droid", command=command)

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", *composer_clear_sequence("droid"), "enter", *["enter"] * enters]
    assert pane.typed == [f"{command}\n"]


@pytest.mark.asyncio
async def test_a_composer_the_enter_empties_is_submitted_once() -> None:
    pane = _UnsubmittedPane(recovers_after=0)

    result, _mark, _clear = await _send(
        pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
    )

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", *composer_clear_sequence("claude"), "enter"]
    assert pane.typed == ["/compact\n"]


@pytest.mark.asyncio
async def test_a_command_the_paste_kept_is_submitted_by_the_enter() -> None:
    pane = _UnsubmittedPane(recovers_after=1)

    result, _mark, clear = await _send(
        pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
    )

    assert result == (True, None, True, None)
    assert pane.typed == ["/compact\n"]
    assert pane.keys == ["escape", *composer_clear_sequence("claude"), "enter"]
    clear.assert_not_called()


async def test_command_the_recovery_enter_cannot_submit_fails_without_retyping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.04)
    pane = _RetypeOnlyPane()

    result, _mark, clear = await _send(
        pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
    )

    compacted, reason, continuation_pending, detail = result
    assert compacted is False
    assert continuation_pending is False
    assert detail == {
        "error_code": _COMMAND_NOT_SUBMITTED_ERROR_CODE,
        "continuation_pending": False,
    }
    assert reason is not None and "/compact" in reason
    assert pane.typed == ["/compact\n"]
    assert pane.keys == [
        "escape",
        *composer_clear_sequence("claude"),
        "enter",
        "enter",
    ]
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_command_that_never_leaves_the_composer_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.04)
    pane = _UnsubmittedPane()

    with caplog.at_level(logging.ERROR, logger=_COMPACTION):
        result, _mark, clear = await _send(
            pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
        )

    compacted, reason, continuation_pending, detail = result
    assert compacted is False
    assert continuation_pending is False
    assert detail == {
        "error_code": _COMMAND_NOT_SUBMITTED_ERROR_CODE,
        "continuation_pending": False,
    }
    assert reason is not None and "/compact" in reason
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "handoff_continuation_not_submitted"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert pane.typed == ["/compact\n"]
    assert pane.keys == [
        "escape",
        *composer_clear_sequence("claude"),
        "enter",
        "enter",
    ]
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_unsubmitted_command_records_no_handoff_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_COMPACTION}._SUBMIT_VERIFY_SETTLE_SECONDS", _SETTLE)
    pane = _UnsubmittedPane()
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(id="session-1", source="claude")
    record = MagicMock(return_value=True)

    with (
        patch(f"{_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_DELIVERY}.composer_reader", return_value=_CLAUDE_READ),
        patch(f"{_DELIVERY}.mark_handoff_compact_continuation_pending", return_value=True),
        patch(f"{_DELIVERY}.clear_handoff_compact_continuation_pending", return_value=True),
        patch(f"{_DELIVERY}.clear_queued_context"),
        patch("gobby.sessions.compact_continuation.record_handoff_delivery", record),
    ):
        result = await deliver_staged_compact_handoff(
            "session-1",
            "a" * 32,
            "handoff-1",
            session_manager=session_manager,
            db=MagicMock(),
            agent_run_manager=MagicMock(),
        )

    assert result["compacted"] is False
    assert result["error_code"] == _COMMAND_NOT_SUBMITTED_ERROR_CODE
    record.assert_not_called()


@pytest.mark.asyncio
async def test_departed_seat_gets_no_keys_and_no_retry_guidance() -> None:
    pane = _ComposerPane()
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(id="session-1", source="codex")

    with (
        patch(f"{_DELIVERY}.recorded_seat_left", return_value=True),
        patch(f"{_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
    ):
        result = await deliver_staged_compact_handoff(
            "session-1",
            "a" * 32,
            "handoff-1",
            session_manager=session_manager,
            db=MagicMock(),
            agent_run_manager=MagicMock(),
        )

    assert result["compacted"] is False
    assert result["error_code"] == NO_TERMINAL_TARGET_ERROR_CODE
    assert pane.keys == []
    assert pane.typed == []


@pytest.mark.asyncio
async def test_interrupt_stops_pressing_once_the_seat_leaves() -> None:
    pane = _ComposerPane()
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)

    ok, _reason, _pending, detail = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="claude",
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=lambda: False,
        settle_seconds=_SETTLE,
        seat_left=lambda: len(pane.keys) > 0,
    )

    assert ok is False
    assert detail is not None
    assert detail["error_code"] == NO_TERMINAL_TARGET_ERROR_CODE
    assert pane.keys == ["escape"]
    assert pane.typed == []
    clear.assert_called_once()


async def test_seat_leaving_during_the_settle_wait_gets_no_first_interrupt() -> None:
    pane = _ComposerPane()
    clear = MagicMock(return_value=True)
    polls: list[None] = []

    def turn_settled() -> bool:
        polls.append(None)
        return False

    ok, _reason, _pending, detail = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=clear,
        observe_interrupt=lambda: False,
        settle_seconds=_SETTLE,
        turn_settled=turn_settled,
        seat_left=lambda: len(polls) > 1,
    )

    assert ok is False
    assert detail == {"error_code": NO_TERMINAL_TARGET_ERROR_CODE, "continuation_pending": False}
    assert pane.keys == []
    assert pane.typed == []
    clear.assert_called_once()


@pytest.mark.parametrize("command", ["/compact", "/clear"])
async def test_seat_leaving_after_the_command_write_gets_no_enter(command: str) -> None:
    # A settled turn skips the interrupt; the CLI exits during the submit gap.
    pane = _ComposerPane()
    clear = MagicMock(return_value=True)

    ok, _reason, _pending, detail = await _send_terminal_compaction_command(
        pane,
        command,
        "session-1",
        cli_source="codex",
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=clear,
        observe_interrupt=lambda: False,
        settle_seconds=_SETTLE,
        turn_settled=lambda: True,
        seat_left=lambda: bool(pane.typed),
    )

    assert ok is False
    assert detail == {"error_code": NO_TERMINAL_TARGET_ERROR_CODE, "continuation_pending": False}
    assert pane.keys == list(composer_clear_sequence("codex"))
    assert pane.typed == [f"{command}\n"]
    clear.assert_called_once()


async def test_codex_departing_during_its_one_press_stops_delivery() -> None:
    pane = _ComposerPane()

    ok, _reason, _pending, detail = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=MagicMock(return_value=True),
        observe_interrupt=lambda: False,
        settle_seconds=_SETTLE,
        turn_settled=lambda: False,
        seat_left=lambda: "escape" in pane.keys,
    )

    assert ok is False
    assert detail == {"error_code": NO_TERMINAL_TARGET_ERROR_CODE, "continuation_pending": False}
    assert pane.keys == ["escape"]
    assert pane.typed == []


async def test_codex_gets_one_safe_interrupt_press_when_none_is_confirmed() -> None:
    pane = _ComposerPane()
    clear = MagicMock(return_value=True)

    ok, reason, _pending, detail = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=clear,
        observe_interrupt=lambda: False,
        settle_seconds=_SETTLE,
        turn_settled=lambda: False,
    )

    assert ok is False
    assert reason == "CLI did not confirm interruption after 1 attempts"
    assert detail is not None
    assert detail["error_code"] == _INTERRUPT_UNCONFIRMED_ERROR_CODE
    assert pane.keys == ["escape"]
    assert pane.typed == []
    clear.assert_called_once()


async def test_settled_codex_with_no_interrupt_observation_never_interrupts() -> None:
    pane = _ComposerPane()
    observe = MagicMock(return_value=False)

    ok, reason, _pending, _detail = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=MagicMock(),
        observe_interrupt=observe,
        turn_settled=lambda: True,
        settle_seconds=0,
    )

    assert ok is True
    assert reason is None
    assert pane.typed == ["/compact\n"]
    assert pane.keys == [*composer_clear_sequence("codex"), "enter"]
    observe.assert_not_called()


async def test_codex_turn_ending_under_its_one_press_proceeds_to_the_command() -> None:
    # The press lands as the turn completes: no turn_aborted is recorded, but the
    # rollout shows the turn ended, which settles the interrupt without a second press.
    pane = _ComposerPane()

    ok, _reason, _pending, _detail = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=MagicMock(return_value=True),
        clear_continuation_pending=MagicMock(return_value=True),
        observe_interrupt=lambda: False,
        settle_seconds=_SETTLE,
        turn_settled=lambda: "escape" in pane.keys,
    )

    assert ok is True
    assert pane.keys.count("escape") == 1
    assert pane.typed == ["/compact\n"]


@pytest.mark.asyncio
async def test_unconfirmed_interrupt_types_nothing_and_clears_the_continuation() -> None:
    pane = _ComposerPane()

    result, mark, clear = await _send(pane, lambda: False)

    ok, reason, pending, detail = result
    assert ok is False
    assert reason == f"CLI did not confirm interruption after {_INTERRUPT_ATTEMPTS} attempts"
    assert pending is False
    assert detail == {"error_code": "interrupt_unconfirmed", "continuation_pending": False}
    assert pane.keys == ["escape"] * _INTERRUPT_ATTEMPTS
    assert pane.typed == []
    mark.assert_called_once()
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_lost_observation_fails_closed_before_typing() -> None:
    pane = _ComposerPane()

    result, _mark, clear = await _send(pane, lambda: None)

    ok, reason, _pending, detail = result
    assert ok is False
    assert reason == "transcript became unavailable during interrupt confirmation"
    assert detail is not None
    assert detail["error_code"] == "interrupt_observation_unavailable"
    assert pane.keys == ["escape"]
    assert pane.typed == []
    clear.assert_called_once()


def test_resolve_pane_io_prefers_the_live_terminal_row() -> None:
    terminal = MagicMock(backend="native", id="term-1")
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = terminal
    registry = MagicMock()

    pane, error = _terminal._resolve_pane_io(
        "session-1",
        MagicMock(),
        MagicMock(),
        terminal_manager=terminal_manager,
        terminal_runtime_registry=registry,
    )

    assert error is None
    assert isinstance(pane, RuntimePaneIO)
    assert (pane.backend, pane.target) == ("native", "term-1")
    registry.resolve.assert_called_once_with("native")


def test_resolve_pane_io_uses_gterm_terminal_named_by_context() -> None:
    terminal_id = "11111111-1111-4111-8111-111111111111"
    terminal = MagicMock(
        backend="native",
        id=terminal_id,
        state="live",
        project_id="proj-1",
        agent_run_id=None,
        session_id=None,
    )
    session = MagicMock(
        id="session-1",
        project_id="proj-1",
        terminal_context={"gobby_terminal_id": terminal_id, "tmux_pane": None},
    )
    session_manager = MagicMock()
    session_manager.get.return_value = session
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = None
    terminal_manager.get.return_value = terminal
    registry = MagicMock()

    pane, error = _terminal._resolve_pane_io(
        "session-1",
        session_manager,
        MagicMock(),
        terminal_manager=terminal_manager,
        terminal_runtime_registry=registry,
    )

    assert error is None
    assert isinstance(pane, RuntimePaneIO)
    assert (pane.backend, pane.target) == ("native", terminal_id)


def test_resolve_pane_io_requires_a_managed_terminal() -> None:
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = None

    pane, error = _terminal._resolve_pane_io(
        "session-1",
        MagicMock(),
        MagicMock(),
        terminal_manager=terminal_manager,
        terminal_runtime_registry=MagicMock(),
    )
    assert pane is None
    assert error == "No live managed terminal for session session-1"

    assert _terminal._resolve_pane_io(
        "session-1",
        MagicMock(),
        MagicMock(),
        terminal_manager=None,
        terminal_runtime_registry=None,
    ) == (None, error)

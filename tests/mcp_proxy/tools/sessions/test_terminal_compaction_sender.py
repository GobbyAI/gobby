"""Verified handoff command delivery: confirm the interrupt, clear, verify, then Enter."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.mcp_proxy.tools.sessions import _terminal
from gobby.mcp_proxy.tools.sessions._terminal_compaction import (
    _COMMAND_NOT_SUBMITTED_ERROR_CODE,
    _INTERRUPT_ATTEMPTS,
    _send_terminal_compaction_command,
)
from gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery import (
    deliver_staged_compact_handoff,
)
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.pane_io import ComposerReader, RuntimePaneIO, TmuxPaneIO
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
    """Pane that redraws Droid's confirm modal over the screen once a command is submitted."""

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        if self.typed and self.keys[-1:] == ["enter"]:
            return "Confirm /compress\nEnter to confirm, ESC to cancel"
        return "output\n> "


class _UnsubmittedPane(_ComposerPane):
    """Claude pane that keeps the typed command after Enter, as a busy composer does.

    The composer empties once ``recovers_after`` Enters have been sent, which models
    a CLI that was waiting for one more Enter; ``None`` never submits.
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
    assert pane.typed == ["/clear"]
    mark.assert_called_once()
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_codex_uses_ctrl_c_and_the_line_drain() -> None:
    pane = _ComposerPane()

    result, _mark, _clear = await _send(pane, lambda: True, cli_source="codex")

    assert result == (True, None, True, None)
    assert pane.keys == ["ctrl_c", *composer_clear_sequence("codex"), "enter"]
    assert pane.typed == ["/clear"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "enters"), [("/compress", 2), ("/clear", 1)])
async def test_droid_presses_enter_on_the_compress_confirm_modal_only(
    command: str, enters: int
) -> None:
    pane = _ConfirmModalPane()

    result, _mark, _clear = await _send(pane, lambda: True, cli_source="droid", command=command)

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", *composer_clear_sequence("droid"), *["enter"] * enters]
    assert pane.typed == [command]


@pytest.mark.asyncio
async def test_composer_emptying_after_enter_submits_once() -> None:
    pane = _UnsubmittedPane(recovers_after=1)

    result, _mark, _clear = await _send(
        pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
    )

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", *composer_clear_sequence("claude"), "enter"]
    assert pane.typed == ["/compact"]


@pytest.mark.asyncio
async def test_command_left_in_the_composer_gets_a_second_enter() -> None:
    pane = _UnsubmittedPane(recovers_after=2)

    result, _mark, clear = await _send(
        pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
    )

    assert result == (True, None, True, None)
    assert pane.typed == ["/compact"]
    assert pane.keys == ["escape", *composer_clear_sequence("claude"), "enter", "enter"]
    clear.assert_not_called()


async def test_command_the_second_enter_cannot_submit_is_retyped() -> None:
    pane = _RetypeOnlyPane()

    result, _mark, clear = await _send(
        pane, lambda: True, command="/compact", composer_read=_CLAUDE_READ
    )

    assert result == (True, None, True, None)
    assert pane.typed == ["/compact", "/compact"]
    assert pane.keys == [
        "escape",
        *composer_clear_sequence("claude"),
        "enter",
        "enter",
        *composer_clear_sequence("claude"),
        "enter",
    ]
    clear.assert_not_called()


@pytest.mark.asyncio
async def test_command_that_never_leaves_the_composer_fails_typed() -> None:
    pane = _UnsubmittedPane()

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
    assert pane.typed == ["/compact", "/compact"]
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
        patch(f"{_DELIVERY}.record_handoff_delivery", record),
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

    with patch.object(_terminal, "_resolve_tmux_target") as resolve_tmux:
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
    resolve_tmux.assert_not_called()


def test_resolve_pane_io_falls_back_to_raw_tmux() -> None:
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = None
    tmux = MagicMock()

    with patch.object(_terminal, "_resolve_tmux_target", return_value=("%4", tmux, None)):
        pane, error = _terminal._resolve_pane_io(
            "session-1",
            MagicMock(),
            MagicMock(),
            terminal_manager=terminal_manager,
            terminal_runtime_registry=MagicMock(),
        )
    assert error is None
    assert isinstance(pane, TmuxPaneIO)
    assert (pane.backend, pane.target) == ("tmux", "%4")

    with patch.object(_terminal, "_resolve_tmux_target", return_value=(None, None, "no pane")):
        assert _terminal._resolve_pane_io(
            "session-1",
            MagicMock(),
            MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        ) == (None, "no pane")

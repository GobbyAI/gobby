"""Verified handoff command delivery: confirm the interrupt, clear, verify, then Enter."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.sessions import _terminal
from gobby.mcp_proxy.tools.sessions._terminal_tmux import (
    _INTERRUPT_ATTEMPTS,
    _send_terminal_compaction_command,
)
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.pane_io import RuntimePaneIO, TmuxPaneIO

pytestmark = pytest.mark.unit

_SETTLE = 0.02


class _ComposerPane:
    """PaneIO fake whose prompt line shows the residue plus whatever was typed."""

    backend = "native"
    target = "term-1"

    def __init__(self, *, residue: str = "") -> None:
        self.residue = residue
        self.line = ""
        self.keys: list[str] = []
        self.typed: list[str] = []

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        if key == "backspace" and self.line:
            self.line = self.line[:-1]
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        self.line += text
        return True, None

    async def snapshot(self, lines: int = 12) -> str | None:
        return f"output\n> {self.residue}{self.line}"


async def _send(
    pane: _ComposerPane,
    observe: Callable[[], bool | None],
    *,
    cli_source: str = "claude",
) -> tuple[tuple[bool, str | None, bool, dict[str, object] | None], MagicMock, MagicMock]:
    mark = MagicMock(return_value=True)
    clear = MagicMock(return_value=True)
    result = await _send_terminal_compaction_command(
        pane,
        "/clear",
        "session-1",
        cli_source=cli_source,
        mark_continuation_pending=mark,
        clear_continuation_pending=clear,
        observe_interrupt=observe,
        settle_seconds=_SETTLE,
    )
    return result, mark, clear


@pytest.mark.asyncio
async def test_confirmed_interrupt_clears_verifies_then_submits_once() -> None:
    pane = _ComposerPane()

    result, mark, clear = await _send(pane, lambda: True)

    assert result == (True, None, True, None)
    assert pane.keys == ["escape", "ctrl_l", "enter"]
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


@pytest.mark.asyncio
async def test_prompt_line_mismatch_backs_the_command_out() -> None:
    pane = _ComposerPane(residue="draft")

    result, _mark, clear = await _send(pane, lambda: True)

    ok, reason, pending, detail = result
    assert ok is False
    assert reason == "composer did not show /clear cleanly before submit"
    assert pending is False
    assert detail == {
        "error_code": "composer_not_clean",
        "continuation_pending": False,
        "prompt_line": "> draft/clear",
    }
    assert "enter" not in pane.keys
    assert pane.keys.count("backspace") == len("/clear")
    assert pane.line == ""
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

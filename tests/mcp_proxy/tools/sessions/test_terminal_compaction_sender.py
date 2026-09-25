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
_CODEX_READ = IdleDetector(BundledDetectionRegistry(), "codex").composer_read
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
        self.draft = ""

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        if key in {"enter", "ctrl_u", "ctrl_k"}:
            self.draft = ""
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        if not text.endswith("\n"):
            self.draft = text
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        return "\n".join(["output", _RULE, f"> {self.draft}", _RULE, "status"])


async def _codex_foreground() -> str:
    return "codex"


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
        composer_read=_CODEX_READ if cli_source == "codex" else composer_read,
        foreground_command=_codex_foreground if cli_source == "codex" else None,
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
async def test_codex_uses_ctrl_c_and_the_line_drain() -> None:
    pane = _ComposerPane()

    result, _mark, _clear = await _send(pane, lambda: True, cli_source="codex")

    assert result == (True, None, True, None)
    assert pane.keys == ["ctrl_c", *composer_clear_sequence("codex"), "enter"]
    assert pane.typed == ["/clear"]


async def test_idle_codex_goal_successor_never_gets_a_second_unconfirmed_ctrl_c() -> None:
    pane = _ComposerPane()
    settled_checks = iter([True, False])

    def turn_settled() -> bool:
        return next(settled_checks, False)

    result = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=lambda: True,
        clear_continuation_pending=lambda: True,
        observe_interrupt=lambda: False,
        turn_settled=turn_settled,
        settle_seconds=_SETTLE,
        composer_read=_CODEX_READ,
        foreground_command=_codex_foreground,
    )

    assert result[0] is False
    assert result[3] == {
        "error_code": "interrupt_unconfirmed",
        "continuation_pending": False,
    }
    assert pane.keys == ["ctrl_c"]
    assert pane.typed == []


@pytest.mark.asyncio
async def test_codex_exits_between_foreground_check_and_write_without_shell_enter() -> None:
    class ExitingPane(_ComposerPane):
        foreground = "codex"

        async def foreground_command(self) -> str:
            return self.foreground

        async def type_text(self, text: str) -> tuple[bool, str | None]:
            self.foreground = "zsh"
            return await super().type_text(text)

    pane = ExitingPane()
    clear = MagicMock(return_value=True)
    result = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=lambda: True,
        clear_continuation_pending=clear,
        observe_interrupt=lambda: False,
        turn_settled=lambda: True,
        composer_read=_CODEX_READ,
        foreground_command=pane.foreground_command,
        settle_seconds=_SETTLE,
    )

    assert result == (
        False,
        "codex is not foreground (found zsh)",
        False,
        {"error_code": "cli_not_foreground", "continuation_pending": False},
    )
    assert pane.typed == ["/compact"]
    assert "enter" not in pane.keys
    assert pane.draft == ""
    clear.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_frame", ["", "/comp"])
async def test_codex_draft_must_be_visible_before_enter(draft_frame: str) -> None:
    class MissingDraftPane(_ComposerPane):
        render_draft = False

        async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str:
            if not self.render_draft:
                shown = draft_frame if self.draft else ""
                return "\n".join(["output", _RULE, f"> {shown}", _RULE, "status"])
            return (await super().snapshot(lines, mode=mode)) or ""

    pane = MissingDraftPane()
    result = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=lambda: True,
        clear_continuation_pending=lambda: True,
        observe_interrupt=lambda: False,
        turn_settled=lambda: True,
        composer_read=_CODEX_READ,
        foreground_command=_codex_foreground,
        settle_seconds=_SETTLE,
    )

    assert result[0] is False
    assert result[3] == {
        "error_code": "command_not_submitted",
        "continuation_pending": False,
    }
    assert pane.typed == ["/compact"]
    assert "enter" not in pane.keys
    assert pane.draft == ""

    pane.render_draft = True
    retry = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=lambda: True,
        clear_continuation_pending=lambda: True,
        observe_interrupt=lambda: False,
        turn_settled=lambda: True,
        composer_read=_CODEX_READ,
        foreground_command=_codex_foreground,
        settle_seconds=_SETTLE,
    )
    assert retry == (True, None, True, {"interrupted": False})
    assert pane.typed == ["/compact", "/compact"]


async def test_compaction_refuses_a_tmux_pane_that_returned_to_zsh() -> None:
    class ExitedTmux:
        async def list_panes(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    pane_id="%12",
                    session_name="codex-seat",
                    pane_dead=False,
                    pane_command="zsh",
                )
            ]

    pane = TmuxPaneIO(ExitedTmux(), "%12")
    result = await _send_terminal_compaction_command(
        pane,
        "/compact",
        "session-1",
        cli_source="codex",
        mark_continuation_pending=lambda: True,
        clear_continuation_pending=lambda: True,
        observe_interrupt=lambda: False,
        turn_settled=lambda: True,
        foreground_command=pane.foreground_command,
        settle_seconds=_SETTLE,
    )

    assert result == (
        False,
        "codex is not foreground (found zsh)",
        False,
        {"error_code": "cli_not_foreground", "continuation_pending": False},
    )


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
) -> None:
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.04)
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

    with patch.object(_terminal, "_resolve_tmux_target") as resolve_tmux:
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

"""PaneIO adapters over the terminal runtime and raw tmux, the composer drain, and
the verified-submit ladder every daemon-driven injection presses Enter through."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.terminals.composer import composer_clear_sequence
from gobby.terminals.pane_io import (
    TEXT_NOT_SUBMITTED_ERROR_CODE,
    PaneIO,
    RuntimePaneIO,
    SubmitResult,
    TmuxPaneIO,
    clear_composer,
    submit_text,
)
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    SnapshotMode,
    SnapshotResult,
    TerminalRuntime,
    TerminalWriteError,
)


class _FakeRuntime:
    def __init__(self, *, key_outcome: Any = None, text_outcome: Any = None) -> None:
        self.keys: list[str] = []
        self.texts: list[tuple[str, bool]] = []
        self.key_outcome = key_outcome if key_outcome is not None else Delivered()
        self.text_outcome = text_outcome if text_outcome is not None else Delivered()
        self.snapshot_text: str | None = "> "
        self.snapshot_modes: list[SnapshotMode] = []

    async def write_key(self, terminal: Any, key: str) -> Any:
        self.keys.append(key)
        if isinstance(self.key_outcome, Exception):
            raise self.key_outcome
        return self.key_outcome

    async def write_text(self, terminal: Any, text: str, *, submit: bool) -> Any:
        self.texts.append((text, submit))
        if isinstance(self.text_outcome, Exception):
            raise self.text_outcome
        return self.text_outcome

    async def snapshot(
        self, terminal: Any, lines: int, *, mode: SnapshotMode = "text"
    ) -> SnapshotResult:
        self.snapshot_modes.append(mode)
        if self.snapshot_text is None:
            raise RuntimeError("no snapshot")
        return SnapshotResult(
            text=self.snapshot_text, truncated=False, dropped_bytes=None, total_bytes=None
        )


class _Terminal:
    id = "term-1"
    backend = "native"


class _FakeTmux:
    def __init__(self, *, dispatch_ok: bool = True, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.dispatch_ok = dispatch_ok
        self.error = error
        self.capture: str | None = "> "
        self.snapshot_modes: list[SnapshotMode] = []

    async def dispatch_keys(self, target: str, keys: str, *, literal: bool) -> bool:
        self.calls.append((target, keys, literal))
        if self.error is not None:
            raise self.error
        return self.dispatch_ok

    async def snapshot_lines(self, target: str, *, lines: int, mode: SnapshotMode = "text") -> str:
        self.snapshot_modes.append(mode)
        if self.capture is None:
            raise RuntimeError("pane gone")
        return self.capture


@pytest.mark.asyncio
async def test_runtime_pane_sends_named_keys_and_unsubmitted_text() -> None:
    runtime = _FakeRuntime()
    pane = RuntimePaneIO(cast(TerminalRuntime, runtime), _Terminal())

    assert pane.backend == "native"
    assert pane.target == "term-1"
    assert await pane.send_key("ctrl_l") == (True, None)
    assert await pane.type_text("/clear") == (True, None)
    assert runtime.keys == ["ctrl_l"]
    assert runtime.texts == [("/clear", False)]
    assert await pane.snapshot(1) == "> "
    assert await pane.snapshot(1, mode="ansi") == "> "
    assert runtime.snapshot_modes == ["text", "ansi"]


@pytest.mark.asyncio
async def test_runtime_pane_submits_text_ending_in_a_newline_like_tmux() -> None:
    runtime = _FakeRuntime()
    pane = RuntimePaneIO(cast(TerminalRuntime, runtime), _Terminal())

    assert await pane.type_text("Call get_handoff()\n") == (True, None)
    assert runtime.texts == [("Call get_handoff()", True)]


@pytest.mark.asyncio
async def test_runtime_pane_reports_indeterminate_and_typed_failures() -> None:
    indeterminate = RuntimePaneIO(
        cast(TerminalRuntime, _FakeRuntime(key_outcome=IndeterminateWrite("lost"))), _Terminal()
    )
    ok, reason = await indeterminate.send_key("escape")
    assert ok is False
    assert reason is not None and "indeterminate" in reason and "lost" in reason

    failed = RuntimePaneIO(
        cast(TerminalRuntime, _FakeRuntime(text_outcome=TerminalWriteError(stage="partial"))),
        _Terminal(),
    )
    ok, reason = await failed.type_text("/clear")
    assert ok is False
    assert reason == "native text write failed (partial)"


@pytest.mark.asyncio
async def test_runtime_pane_snapshot_failure_returns_none() -> None:
    runtime = _FakeRuntime()
    runtime.snapshot_text = None

    assert await RuntimePaneIO(cast(TerminalRuntime, runtime), _Terminal()).snapshot(3) is None


@pytest.mark.asyncio
async def test_tmux_pane_maps_named_keys_and_types_literal_text() -> None:
    tmux = _FakeTmux()
    pane = TmuxPaneIO(tmux, "%7")

    assert pane.backend == "tmux"
    assert pane.target == "%7"
    assert await pane.send_key("ctrl_l") == (True, None)
    assert await pane.send_key("backspace") == (True, None)
    assert await pane.type_text("/clear") == (True, None)
    assert tmux.calls == [("%7", "C-l", False), ("%7", "BSpace", False), ("%7", "/clear", True)]
    assert await pane.snapshot(2) == "> "
    assert await pane.snapshot(2, mode="ansi") == "> "
    assert tmux.snapshot_modes == ["text", "ansi"]


@pytest.mark.asyncio
async def test_tmux_pane_rejects_keys_tmux_cannot_name() -> None:
    ok, reason = await TmuxPaneIO(_FakeTmux(), "%7").send_key("kp0")

    assert ok is False
    assert reason == "tmux has no key name for kp0"


@pytest.mark.asyncio
async def test_tmux_pane_reports_dispatch_failures() -> None:
    ok, reason = await TmuxPaneIO(_FakeTmux(dispatch_ok=False), "%7").send_key("enter")
    assert ok is False
    assert reason == "tmux send-keys returned false while sending enter to %7"

    ok, reason = await TmuxPaneIO(_FakeTmux(error=TimeoutError()), "%7").type_text("x")
    assert ok is False
    assert reason == "tmux send-keys timed out while typing text to %7"

    tmux = _FakeTmux()
    tmux.capture = None
    assert await TmuxPaneIO(tmux, "%7").snapshot(1) is None


class _RecordingPane:
    """PaneIO fake that records keys and counts snapshots."""

    backend = "fake"
    target = "pane"

    def __init__(self, *, fail_key: str | None = None) -> None:
        self.fail_key = fail_key
        self.keys: list[str] = []
        self.snapshots = 0

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        if key == self.fail_key:
            return False, f"{key} failed"
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        self.snapshots += 1
        return "> "


@pytest.mark.asyncio
async def test_clear_composer_sends_the_drain_blind() -> None:
    pane = _RecordingPane()

    assert await clear_composer(pane, "claude") == (True, None)
    assert pane.keys == list(composer_clear_sequence("claude"))
    assert pane.snapshots == 0


@pytest.mark.asyncio
async def test_clear_composer_stops_at_the_first_failed_key() -> None:
    pane = _RecordingPane(fail_key="ctrl_k")

    assert await clear_composer(pane, "codex") == (False, "ctrl_k failed")
    assert pane.keys == ["ctrl_u", "ctrl_k"]


#: Longer than COMPOSER_MATCH_CHARS, so a held draft matches on its first row only.
_TEXT = "Call get_handoff() on gobby-sessions, then continue."


class _ScriptedPane:
    """PaneIO fake whose composer reads follow a script, one entry per probe.

    The last entry repeats, so a one-entry script is a composer that never changes.
    """

    backend = "fake"
    target = "pane"

    def __init__(self, reads: list[ComposerRead]) -> None:
        self._reads = reads
        self.probes = 0
        self.keys: list[str] = []
        self.typed: list[str] = []

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        self.typed.append(text)
        return True, None

    async def snapshot(self, lines: int = 12, *, mode: SnapshotMode = "text") -> str | None:
        return None

    def read(self, _snapshot: str | None) -> ComposerRead:
        read = self._reads[min(self.probes, len(self._reads) - 1)]
        self.probes += 1
        return read


async def _submit(
    pane: _ScriptedPane, monkeypatch: pytest.MonkeyPatch, *, verify_seconds: float = 0.0
) -> SubmitResult:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_ENTER_GAP_SECONDS", 0.0)
    monkeypatch.setattr("gobby.terminals.pane_io.asyncio.sleep", no_sleep)
    return await submit_text(
        cast(PaneIO, pane),
        _TEXT,
        "session-1",
        label="the prompt",
        cli_source="claude",
        composer_read=pane.read,
        verify_seconds=verify_seconds,
    )


@pytest.mark.asyncio
async def test_the_write_and_a_delayed_enter_submit_and_an_empty_composer_proves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One write carries the text and its newline; the Enter is its own read after it.

    The composer is read only after the Enter: a read taken right after the write is
    ``empty`` before the CLI has rendered the write, and trusting it is what stranded
    three live pull prompts.
    """
    pane = _ScriptedPane([ComposerRead("empty")])

    assert (await _submit(pane, monkeypatch)).ok is True
    assert pane.typed == [f"{_TEXT}\n"]
    assert pane.keys == ["enter"]
    assert pane.probes == 1


@pytest.mark.asyncio
async def test_a_repaint_after_enter_is_polled_until_the_composer_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _ScriptedPane([ComposerRead("unknown"), ComposerRead("empty")])

    assert (await _submit(pane, monkeypatch, verify_seconds=1.0)).ok is True
    assert pane.typed == [f"{_TEXT}\n"]
    assert pane.keys == ["enter"]


@pytest.mark.asyncio
async def test_an_unreadable_composer_trusts_the_delivered_write_and_enter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No frame after the Enter is no evidence of a failure.

    The write and the key were both delivered. Retyping into a composer that may have
    taken them would queue the text twice, so the ladder reports it submitted and
    says why.
    """
    pane = _ScriptedPane([ComposerRead("unknown")])

    with caplog.at_level(logging.WARNING, logger="gobby.terminals.pane_io"):
        result = await _submit(pane, monkeypatch)

    assert result.ok is True
    assert pane.typed == [f"{_TEXT}\n"]
    assert pane.keys == ["enter"]
    assert "could not be read after submitting the prompt" in caplog.text


@pytest.mark.asyncio
async def test_held_draft_is_submitted_by_a_second_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _ScriptedPane([ComposerRead("draft", _TEXT), ComposerRead("empty")])

    assert (await _submit(pane, monkeypatch)).ok is True
    assert pane.typed == [f"{_TEXT}\n"]
    assert pane.keys == ["enter", "enter"]


@pytest.mark.asyncio
async def test_held_draft_after_the_retry_budget_reports_command_not_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _ScriptedPane([ComposerRead("draft", _TEXT)])
    monkeypatch.setattr("gobby.terminals.pane_io.SUBMIT_HELD_RETRY_SECONDS", 0.02)

    result = await _submit(pane, monkeypatch, verify_seconds=0.01)

    assert result.ok is False
    assert result.error_code == TEXT_NOT_SUBMITTED_ERROR_CODE
    assert pane.typed == [f"{_TEXT}\n"]
    assert pane.keys == ["enter", "enter"]


@pytest.mark.asyncio
async def test_an_unreadable_composer_after_a_held_read_trusts_the_enter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pane = _ScriptedPane([ComposerRead("draft", _TEXT), ComposerRead("unknown")])

    with caplog.at_level(logging.WARNING, logger="gobby.terminals.pane_io"):
        result = await _submit(pane, monkeypatch)

    assert result.ok is True
    assert pane.typed == [f"{_TEXT}\n"]
    assert pane.keys == ["enter", "enter"]
    assert "re-sending Enter" in caplog.text
    assert "could not be read after submitting the prompt" in caplog.text

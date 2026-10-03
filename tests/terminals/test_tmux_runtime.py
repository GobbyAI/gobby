"""TmuxTerminalRuntime contract tests (plan 2.2)."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.text_injection import (
    AttentionInjectionError,
    TmuxTargetUnavailableError,
    TmuxTextInjectionError,
    TmuxTextInjectionTimeout,
)
from gobby.storage.terminals import Terminal
from gobby.terminals.host_protocol import frames_socket_path
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    SnapshotResult,
    TerminalWriteError,
)
from gobby.terminals.tmux_runtime import (
    InputPayloadTooLargeError,
    TmuxTerminalRuntime,
)
from tests.terminals.fakes import make_memory_terminal

pytestmark = pytest.mark.unit

MAX_INPUT_PAYLOAD = 1024 * 1024


_RECORDED_GENERATION = (1658, 1784592177)
_RESTARTED_GENERATION = (1658, 1790000000)


class _StubSessions(TmuxSessionManager):
    """Session manager whose tmux seams are plain attributes the tests replace.

    The generation probe answers with the fake row's recorded server; every
    other tmux command goes to ``answer``.
    """

    answer: Any
    capture_pane: Any
    capture_full_pane: Any
    has_session: Any

    async def _run(self, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
        if "#{pid}" in args[-1]:
            return 0, "{}\t{}\n".format(*_RECORDED_GENERATION), ""
        result: tuple[int, str, str] = await self.answer(*args, timeout=timeout)
        return result


def _sessions() -> _StubSessions:
    sessions = _StubSessions("/tmp/gobby-test-tmux.sock")
    sessions.answer = AsyncMock(return_value=(0, "", ""))
    return sessions


def _runtime(sessions: TmuxSessionManager, **kwargs: Any) -> TmuxTerminalRuntime:
    return TmuxTerminalRuntime(sessions_for_socket=lambda _socket: sessions, **kwargs)


@pytest.mark.asyncio
async def test_attach_locator_uses_live_host_identity(tmp_path: Path) -> None:
    host = SimpleNamespace(host_epoch="epoch-1", socket_dir=tmp_path)
    runtime = _runtime(_sessions(), host_control=host)
    terminal = make_memory_terminal()

    first = await runtime.attach_locator(terminal)

    assert first.frame_host_epoch == "epoch-1"
    assert first.host_socket == str(frames_socket_path(tmp_path))
    assert first.is_valid_for_direct("tmux") is True

    host.host_epoch = "epoch-2"
    second = await runtime.attach_locator(terminal)

    assert second.frame_host_epoch == "epoch-2"
    assert second.is_valid_for_direct("tmux") is True


@pytest.mark.asyncio
async def test_snapshot_counters_are_utf8_bytes_with_unknown_history_loss() -> None:
    sessions = _sessions()
    runtime = _runtime(sessions)
    terminal = make_memory_terminal()
    wide = "盒🙂"
    sessions.capture_pane = AsyncMock(return_value=wide)
    sessions.capture_full_pane = AsyncMock(return_value=wide)
    sessions.answer = AsyncMock(return_value=(0, "12|10000", ""))

    visible = await runtime.snapshot(terminal, lines=50)
    assert isinstance(visible, SnapshotResult)
    assert visible.text == wide
    assert visible.truncated is False
    assert visible.dropped_bytes == 0
    assert visible.total_bytes == len(wide.encode("utf-8"))
    assert visible.total_bytes != len(wide)

    sessions.answer = AsyncMock(return_value=(0, "10000|10000", ""))
    full = await runtime.snapshot_full(terminal)
    assert full.truncated is True
    assert full.dropped_bytes is None
    assert full.total_bytes is None
    full.text.encode("utf-8")
    snapshot_hints = inspect.signature(TmuxTerminalRuntime.snapshot).return_annotation
    assert snapshot_hints is SnapshotResult or "SnapshotResult" in str(snapshot_hints)


@pytest.mark.asyncio
async def test_snapshot_passes_the_requested_mode_to_capture() -> None:
    sessions = _sessions()
    runtime = _runtime(sessions)
    sessions.capture_pane = AsyncMock(return_value="\x1b[2mfaint\x1b[0m")
    sessions.answer = AsyncMock(return_value=(0, "12|10000", ""))

    styled = await runtime.snapshot(make_memory_terminal(), lines=40, mode="ansi")

    assert styled.text == "\x1b[2mfaint\x1b[0m"
    assert sessions.capture_pane.await_args is not None
    assert sessions.capture_pane.await_args.kwargs == {"lines": 40, "mode": "ansi"}


@pytest.mark.asyncio
async def test_write_returns_indeterminate_when_effect_precedes_lost_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    runtime = _runtime(sessions)
    terminal = make_memory_terminal()
    landed: list[str] = []

    async def paste(*_args: object, **_kwargs: object) -> None:
        landed.append("paste")

    async def timeout_enter(*_args: object, **_kwargs: object) -> None:
        raise TmuxTextInjectionTimeout(command=("tmux", "send-keys"), timeout=10.0)

    monkeypatch.setattr(
        "gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target",
        paste,
    )
    monkeypatch.setattr(
        "gobby.terminals.tmux_runtime.send_enter_key_to_tmux_target",
        timeout_enter,
    )
    monkeypatch.setattr("gobby.terminals.tmux_runtime.asyncio.sleep", AsyncMock())
    outcome = await runtime.write_text(terminal, "hello", submit=True)
    assert isinstance(outcome, IndeterminateWrite)
    assert landed == ["paste"]
    assert outcome is not None

    async def missing(*_args: object, **_kwargs: object) -> None:
        raise TmuxTargetUnavailableError(
            "tmux target is unavailable: no such pane",
            command=("tmux",),
        )

    monkeypatch.setattr(
        "gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target",
        missing,
    )
    with pytest.raises((AttentionInjectionError, Exception)) as exc_info:
        await runtime.write_text(terminal, "hello", submit=False)
    stage = getattr(exc_info.value, "stage", None)
    assert stage == "none"

    delivered = await _delivered_write(runtime, terminal, monkeypatch)
    assert isinstance(delivered, Delivered)


async def _delivered_write(
    runtime: TmuxTerminalRuntime,
    terminal: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    async def ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target",
        ok,
    )
    return await runtime.write_text(terminal, "ok", submit=False)


@pytest.mark.asyncio
async def test_write_paste_follows_live_bracketed_mode_and_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    runtime = _runtime(sessions)
    terminal = make_memory_terminal()
    sent: list[str] = []

    async def query(flag: str) -> None:
        sessions.answer = AsyncMock(return_value=(0, flag, ""))

    async def capture_paste(*args: object, **_kwargs: object) -> None:
        sent.append(str(args[1]))

    monkeypatch.setattr(
        "gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target",
        capture_paste,
    )
    await query("0|0|1")
    await runtime.write_paste(terminal, "line1\nline2")
    assert sent[-1] == "\x1b[200~line1\nline2\x1b[201~"
    await query("0|0|0")
    await runtime.write_paste(terminal, "raw\ntext")
    assert sent[-1] == "raw\ntext"

    oversize = "é" * ((MAX_INPUT_PAYLOAD // len("é".encode())) + 1)
    with pytest.raises(InputPayloadTooLargeError):
        await runtime.write_paste(terminal, oversize)
    assert sent[-1] == "raw\ntext"


@pytest.mark.asyncio
async def test_write_key_encodes_against_live_pane_flags() -> None:
    sessions = _sessions()
    runtime = _runtime(sessions)
    terminal = make_memory_terminal()
    hex_payloads: list[list[str]] = []

    async def run(*args: str, **_kwargs: object) -> tuple[int, str, str]:
        joined = " ".join(args)
        if "display-message" in joined or (args and args[0] == "display-message"):
            return (0, "1|0|0", "")
        if args and args[0] == "send-keys":
            hex_payloads.append(list(args))
            return (0, "", "")
        return (0, "", "")

    sessions.answer = AsyncMock(side_effect=run)
    await runtime.write_key(terminal, "up")
    assert any(part.lower() == "1b" or part == "1b" for cmd in hex_payloads for part in cmd)
    first = hex_payloads[-1]
    assert "4f" in [part.lower() for part in first] or "O" in first

    hex_payloads.clear()

    async def run_normal(*args: str, **_kwargs: object) -> tuple[int, str, str]:
        if args and args[0] == "display-message":
            return (0, "0|1|0", "")
        if args and args[0] == "send-keys":
            hex_payloads.append(list(args))
            return (0, "", "")
        return (0, "", "")

    sessions.answer = AsyncMock(side_effect=run_normal)
    await runtime.write_key(terminal, "up")
    up_normal = hex_payloads[-1]
    assert "5b" in [part.lower() for part in up_normal]

    hex_payloads.clear()
    await runtime.write_key(terminal, "kpplus")
    app_keypad = hex_payloads[-1]

    hex_payloads.clear()

    async def run_normal_keypad(*args: str, **_kwargs: object) -> tuple[int, str, str]:
        if args and args[0] == "display-message":
            return (0, "0|0|0", "")
        if args and args[0] == "send-keys":
            hex_payloads.append(list(args))
            return (0, "", "")
        return (0, "", "")

    sessions.answer = AsyncMock(side_effect=run_normal_keypad)
    await runtime.write_key(terminal, "kpplus")
    assert hex_payloads[-1] != app_keypad


@pytest.mark.asyncio
async def test_session_present_when_remain_on_exit_pane_is_dead() -> None:
    sessions = _sessions()
    runtime = _runtime(sessions)
    terminal = make_memory_terminal(session_name="gobby-orphan")
    sessions.has_session = AsyncMock(return_value=True)
    sessions.answer = AsyncMock(return_value=(0, "1", ""))

    assert await runtime.is_live(terminal) is False
    assert await runtime.session_present(terminal) is True
    sessions.has_session.assert_awaited_once_with("gobby-orphan")


@pytest.mark.asyncio
async def test_terminate_kills_on_the_terminals_own_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External rows probe their own socket, so the kill must target it too."""
    _ExternalTmux(monkeypatch)
    runtime = TmuxTerminalRuntime()
    terminal = make_memory_terminal(session_name="ext-demo")
    killed_sockets: list[str] = []

    async def fake_kill(
        self: TmuxSessionManager, name: str, *, missing_ok: bool = False, timeout: float = 5.0
    ) -> bool:
        killed_sockets.append(self.base_args()[-1])
        return True

    monkeypatch.setattr(TmuxSessionManager, "kill_session", fake_kill)
    await runtime.terminate(terminal, grace_seconds=5.0)
    locator = terminal.locator or {}
    assert killed_sockets == [locator["socket_path"]]


@pytest.mark.asyncio
async def test_write_text_reports_partial_when_enter_fails_after_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A landed paste followed by a failed Enter is a partial write, never a retry."""
    runtime = _runtime(_sessions())
    terminal = make_memory_terminal()

    async def paste(*_args: object, **_kwargs: object) -> None:
        return None

    async def failed_enter(*_args: object, **_kwargs: object) -> None:
        raise TmuxTextInjectionError(
            "enter withheld",
            command=("tmux", "send-keys"),
            stderr="injected",
            returncode=1,
        )

    monkeypatch.setattr("gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target", paste)
    monkeypatch.setattr("gobby.terminals.tmux_runtime.send_enter_key_to_tmux_target", failed_enter)
    monkeypatch.setattr("gobby.terminals.tmux_runtime.asyncio.sleep", AsyncMock())
    with pytest.raises(TerminalWriteError) as exc:
        await runtime.write_text(terminal, "ECHO partial", submit=True)
    assert exc.value.stage == "partial"


class _ExternalTmux:
    """Answers tmux on an external pane's socket and records every command sent.

    Each generation query consumes the next queued ``(server_pid, start_time)``;
    the last one keeps answering.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *generations: tuple[int, int]) -> None:
        self.commands: list[tuple[list[str], tuple[str, ...]]] = []
        self._generations = list(generations or (_RECORDED_GENERATION,))

        async def run(
            manager: TmuxSessionManager, *args: str, timeout: float = 10.0
        ) -> tuple[int, str, str]:
            return self._answer(manager.base_args(), args)

        monkeypatch.setattr(TmuxSessionManager, "_run", run)
        monkeypatch.setattr(
            "gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target", self._paste
        )
        monkeypatch.setattr(
            "gobby.terminals.tmux_runtime.send_enter_key_to_tmux_target", self._enter
        )
        monkeypatch.setattr(
            "gobby.terminals.tmux_runtime.send_named_key_to_tmux_target", self._named
        )
        monkeypatch.setattr("gobby.terminals.tmux_runtime.asyncio.sleep", AsyncMock())

    def _generation(self) -> tuple[int, int]:
        if len(self._generations) > 1:
            return self._generations.pop(0)
        return self._generations[0]

    def _answer(self, tmux_cmd: list[str], args: tuple[str, ...]) -> tuple[int, str, str]:
        self.commands.append((tmux_cmd, args))
        if args[:1] != ("display-message",):
            return 0, "", ""
        answer = args[-1]
        if "#{pid}" in answer:
            pid, start = self._generation()
            answer = answer.replace("#{pid}", str(pid)).replace("#{start_time}", str(start))
        for flag in ("#{pane_dead}", "#{cursor_keys_flag}", "#{keypad_cursor_flag}"):
            answer = answer.replace(flag, "0")
        return 0, answer.replace("#{bracket_paste_flag}", "0") + "\n", ""

    async def _paste(self, target: str, text: str, *, tmux_cmd: list[str]) -> None:
        self.commands.append((list(tmux_cmd), ("paste", target, text)))

    async def _enter(self, target: str, *, tmux_cmd: list[str]) -> None:
        self.commands.append((list(tmux_cmd), ("enter", target)))

    async def _named(self, target: str, key: str, *, tmux_cmd: list[str]) -> None:
        self.commands.append((list(tmux_cmd), ("named", target, key)))

    def byte_writes(self) -> list[tuple[str, ...]]:
        return [
            args
            for _cmd, args in self.commands
            if args[0] in {"paste", "enter", "named", "send-keys"}
        ]


def _external_terminal(**locator_overrides: object) -> Terminal:
    terminal = make_memory_terminal(session_name="ext-demo")
    locator = {**(terminal.locator or {}), **locator_overrides}
    return replace(terminal, locator={k: v for k, v in locator.items() if v is not None})


async def _write(runtime: TmuxTerminalRuntime, terminal: Terminal, op: str) -> object:
    if op == "text":
        return await runtime.write_text(terminal, "hello", submit=True)
    if op == "key":
        return await runtime.write_key(terminal, "enter")
    if op == "arrow":
        return await runtime.write_key(terminal, "up")
    if op == "paste":
        return await runtime.write_paste(terminal, "hello")
    return await runtime.write_input(terminal, b"hello")


_WRITE_OPS = ["text", "key", "arrow", "paste", "input"]


@pytest.mark.asyncio
@pytest.mark.parametrize("op", _WRITE_OPS)
async def test_external_writes_address_the_panes_own_socket(
    monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    """send_keys, wake and /compact all land here; none may reach the gobby server."""
    tmux = _ExternalTmux(monkeypatch)
    runtime = TmuxTerminalRuntime()
    terminal = _external_terminal()

    assert await _write(runtime, terminal, op) == Delivered()

    assert tmux.byte_writes()
    socket_path = (terminal.locator or {})["socket_path"]
    for tmux_cmd, _args in tmux.commands:
        assert "-L" not in tmux_cmd
        assert tmux_cmd[tmux_cmd.index("-S") + 1] == socket_path


@pytest.mark.asyncio
@pytest.mark.parametrize("op", _WRITE_OPS)
@pytest.mark.parametrize(
    "locator",
    [
        pytest.param({}, id="recycled-pane"),
        pytest.param({"server_pid": None}, id="no-server-pid"),
        pytest.param({"server_start_time": None}, id="no-start-time"),
    ],
)
async def test_external_writes_refuse_a_pane_from_another_server_generation(
    monkeypatch: pytest.MonkeyPatch, op: str, locator: dict[str, object]
) -> None:
    """A restarted server reuses %N; bytes meant for the old pane must not reach it."""
    generation = _RESTARTED_GENERATION if not locator else _RECORDED_GENERATION
    tmux = _ExternalTmux(monkeypatch, generation)
    runtime = TmuxTerminalRuntime()

    with pytest.raises(TerminalWriteError) as exc:
        await _write(runtime, _external_terminal(**locator), op)

    assert exc.value.stage == "none"
    assert tmux.byte_writes() == []


@pytest.mark.asyncio
async def test_delayed_submit_is_withheld_when_the_server_restarts_mid_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = _ExternalTmux(monkeypatch, _RECORDED_GENERATION, _RESTARTED_GENERATION)
    runtime = TmuxTerminalRuntime()

    with pytest.raises(TerminalWriteError) as exc:
        await runtime.write_text(_external_terminal(), "hello", submit=True)

    assert exc.value.stage == "partial"
    assert [args[0] for args in tmux.byte_writes()] == ["paste"]


@pytest.mark.asyncio
async def test_a_recycled_pane_is_not_live_and_is_never_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ExternalTmux(monkeypatch, _RESTARTED_GENERATION)
    kill = AsyncMock(return_value=True)
    monkeypatch.setattr(TmuxSessionManager, "kill_session", kill)
    runtime = TmuxTerminalRuntime()
    terminal = _external_terminal()

    assert await runtime.is_live(terminal) is False
    await runtime.terminate(terminal, grace_seconds=5.0)
    kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_recycled_pane_keeps_its_window_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restarted server's %N is someone else's window; sizing must not touch it."""
    tmux = _ExternalTmux(monkeypatch, _RESTARTED_GENERATION)
    runtime = TmuxTerminalRuntime()
    terminal = _external_terminal()

    await runtime.resize(terminal, rows=40, cols=120)
    await runtime.release_size(terminal)

    assert [args for _cmd, args in tmux.commands if args[0] != "display-message"] == []


@pytest.mark.asyncio
async def test_a_recycled_pane_is_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restarted server's %N is someone else's pane; snapshots must not read it."""
    tmux = _ExternalTmux(monkeypatch, _RESTARTED_GENERATION)
    runtime = TmuxTerminalRuntime()
    terminal = _external_terminal()

    visible = await runtime.snapshot(terminal, lines=50)
    full = await runtime.snapshot_full(terminal)

    assert (visible.text, full.text) == ("", "")
    assert [args[0] for _cmd, args in tmux.commands] == ["display-message", "display-message"]


@pytest.mark.asyncio
async def test_sizing_addresses_the_recorded_panes_own_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmux = _ExternalTmux(monkeypatch)
    runtime = TmuxTerminalRuntime()
    terminal = _external_terminal()

    await runtime.resize(terminal, rows=40, cols=120)
    await runtime.release_size(terminal)

    socket_path = (terminal.locator or {})["socket_path"]
    assert [args for _cmd, args in tmux.commands if args[0] != "display-message"] == [
        ("set-option", "-w", "-t", "%1", "window-size", "manual"),
        ("resize-window", "-t", "%1", "-x", "120", "-y", "40"),
        ("set-option", "-wu", "-t", "%1", "window-size"),
    ]
    assert all(cmd[cmd.index("-S") + 1] == socket_path for cmd, _args in tmux.commands)

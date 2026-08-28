"""TmuxTerminalRuntime contract tests (plan 2.2)."""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionInfo, TmuxSessionManager
from gobby.agents.tmux.text_injection import (
    AttentionInjectionError,
    TmuxTargetUnavailableError,
    TmuxTextInjectionTimeout,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.terminals import AttachLocator
from gobby.terminals.runtime import (
    Delivered,
    IndeterminateWrite,
    SnapshotResult,
    TerminalRuntime,
    TerminalSpawnRequest,
)
from gobby.terminals.tmux_runtime import (
    CommitSpawnRefusedError,
    InputPayloadTooLargeError,
    TmuxTerminalRuntime,
)
from tests.terminals.fakes import make_memory_terminal

pytestmark = pytest.mark.unit

MAX_INPUT_PAYLOAD = 1024 * 1024


class _StubSessions(TmuxSessionManager):
    """Session manager whose tmux seams are plain attributes the tests replace."""

    _run: Any
    is_available: Any
    create_session: Any
    capture_pane: Any
    capture_full_pane: Any


def _sessions() -> _StubSessions:
    sessions = _StubSessions(TmuxConfig(history_limit=10000))
    sessions.is_available = MagicMock(return_value=True)
    return sessions


@pytest.mark.asyncio
async def test_prepare_commit_requires_caller_ack() -> None:
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
    created: list[str] = []

    async def create_session(
        name: str,
        command: str | list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> TmuxSessionInfo:
        del command, cwd, env
        created.append(name)
        return TmuxSessionInfo(name=name, pane_pid=42, pane_id="%9")

    sessions.create_session = create_session
    sessions._run = AsyncMock(return_value=(0, "/tmp/tmux.sock|1658|1784592177|%9", ""))
    request = TerminalSpawnRequest(
        terminal_id=uuid4(),
        spawn_key="gobby-abc",
        command=["echo", "hi"],
        rows=24,
        cols=80,
    )
    prepared = await runtime.prepare_spawn(request)
    assert inspect.signature(runtime.prepare_spawn)
    assert not hasattr(runtime, "spawn") or "spawn" not in TerminalRuntime.__dict__
    with pytest.raises(CommitSpawnRefusedError):
        await runtime.commit_spawn(prepared)
    prepared.acknowledge_persist()
    handle = await runtime.commit_spawn(prepared)
    assert handle.terminal_id == request.terminal_id
    assert isinstance(handle.locator, AttachLocator)
    assert created == ["gobby-abc"]


@pytest.mark.asyncio
async def test_snapshot_counters_are_utf8_bytes_with_unknown_history_loss() -> None:
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
    terminal = make_memory_terminal()
    wide = "盒🙂"
    sessions.capture_pane = AsyncMock(return_value=wide)
    sessions.capture_full_pane = AsyncMock(return_value=wide)
    sessions._run = AsyncMock(return_value=(0, "12|10000", ""))

    visible = await runtime.snapshot(terminal, lines=50)
    assert isinstance(visible, SnapshotResult)
    assert visible.text == wide
    assert visible.truncated is False
    assert visible.dropped_bytes == 0
    assert visible.total_bytes == len(wide.encode("utf-8"))
    assert visible.total_bytes != len(wide)

    sessions._run = AsyncMock(return_value=(0, "10000|10000", ""))
    full = await runtime.snapshot_full(terminal)
    assert full.truncated is True
    assert full.dropped_bytes is None
    assert full.total_bytes is None
    full.text.encode("utf-8")
    snapshot_hints = inspect.signature(TmuxTerminalRuntime.snapshot).return_annotation
    assert snapshot_hints is SnapshotResult or "SnapshotResult" in str(snapshot_hints)


@pytest.mark.asyncio
async def test_write_returns_indeterminate_when_effect_precedes_lost_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
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
    runtime = TmuxTerminalRuntime(sessions)
    terminal = make_memory_terminal()
    sent: list[str] = []

    async def query(flag: str) -> None:
        sessions._run = AsyncMock(return_value=(0, flag, ""))

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
    runtime = TmuxTerminalRuntime(sessions)
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

    sessions._run = AsyncMock(side_effect=run)
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

    sessions._run = AsyncMock(side_effect=run_normal)
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

    sessions._run = AsyncMock(side_effect=run_normal_keypad)
    await runtime.write_key(terminal, "kpplus")
    assert hex_payloads[-1] != app_keypad

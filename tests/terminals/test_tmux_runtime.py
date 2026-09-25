"""TmuxTerminalRuntime contract tests (plan 2.2)."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.spawn_executor import _promote_prepared
from gobby.agents.spawn_executor_providers import ProviderSpawnPlan
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.tmux.session_manager import TmuxSessionInfo, TmuxSessionManager
from gobby.agents.tmux.text_injection import (
    AttentionInjectionError,
    TmuxTargetUnavailableError,
    TmuxTextInjectionTimeout,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.terminals import AttachLocator, TerminalManager
from gobby.terminals.host_protocol import frames_socket_path
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
from gobby.terminals.web_spawn import spawn_web_terminal
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

MAX_INPUT_PAYLOAD = 1024 * 1024


class _StubSessions(TmuxSessionManager):
    """Session manager whose tmux seams are plain attributes the tests replace."""

    _run: Any
    is_available: Any
    create_session: Any
    capture_pane: Any
    capture_full_pane: Any
    has_session: Any


def _sessions() -> _StubSessions:
    sessions = _StubSessions(TmuxConfig(history_limit=10000))
    sessions.is_available = MagicMock(return_value=True)
    return sessions


@pytest.mark.asyncio
async def test_tmux_runtime_reads_foreground_command(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = _sessions()
    lookup = AsyncMock(return_value=TmuxSessionInfo(name="codex-seat", pane_command="zsh"))
    monkeypatch.setattr(sessions, "get_session", lookup)
    runtime = TmuxTerminalRuntime(sessions)
    terminal = replace(make_memory_terminal(session_name="codex-seat"), locator={})

    assert await runtime.foreground_command(terminal) == "zsh"
    lookup.assert_awaited_once_with("codex-seat")


@pytest.mark.asyncio
async def test_tmux_runtime_reads_the_target_pane_in_a_multipane_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions()
    terminal = make_memory_terminal(session_name="codex-seat")
    socket_path = (terminal.locator or {})["socket_path"]
    lookup = AsyncMock(return_value=TmuxSessionInfo(name="codex-seat", pane_command="zsh"))
    panes = AsyncMock(
        return_value=[
            SimpleNamespace(
                pane_id="%1",
                pane_dead=False,
                pane_command="zsh",
                socket_path=socket_path,
                server_pid=1658,
                server_start_time=1784592177,
            ),
            SimpleNamespace(
                pane_id="%2",
                pane_dead=False,
                pane_command="codex",
                socket_path=socket_path,
                server_pid=1658,
                server_start_time=1784592177,
            ),
        ]
    )
    monkeypatch.setattr(sessions, "get_session", lookup)
    monkeypatch.setattr(sessions, "list_panes", panes)
    runtime = TmuxTerminalRuntime(sessions)
    monkeypatch.setattr(runtime, "_sessions_for", lambda _terminal: sessions)
    terminal = replace(
        terminal,
        ownership="external",
        locator={**(terminal.locator or {}), "pane_id": "%2"},
    )

    assert await runtime.foreground_command(terminal) == "codex"
    stale = replace(terminal, locator={**(terminal.locator or {}), "server_pid": 9999})
    assert await runtime.foreground_command(stale) is None
    assert lookup.await_count == 0
    assert panes.await_count == 2


@pytest.mark.asyncio
async def test_attach_locator_uses_live_host_identity(tmp_path: Path) -> None:
    host = SimpleNamespace(host_epoch="epoch-1", socket_dir=tmp_path)
    runtime = TmuxTerminalRuntime(_sessions(), host_control=host)
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
async def test_spawn_geometry_matches_request_and_row() -> None:
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
    manager = MemoryTerminalStore()
    created: list[tuple[int, int]] = []

    async def run(*args: str, **_kwargs: object) -> tuple[int, str, str]:
        if args[0] == "has-session":
            return (1, "", "can't find session")
        if args[0] == "new-session":
            cols = int(args[args.index("-x") + 1])
            rows = int(args[args.index("-y") + 1])
            created.append((cols, rows))
            return (0, "", "")
        if args[-1] == "#{pane_pid}":
            return (0, "42", "")
        if args[-1] == "#{socket_path}|#{pid}|#{start_time}|#{pane_id}":
            return (0, "/tmp/tmux.sock|1658|1784592177|%9", "")
        if args[-1] == "#{pane_height} #{pane_width}":
            cols, rows = created[-1]
            return (0, f"{rows} {cols}", "")
        raise AssertionError(args)

    sessions._run = AsyncMock(side_effect=run)
    project_id = str(uuid4())

    requested = await spawn_web_terminal(
        manager=cast(TerminalManager, manager),
        runtime=runtime,
        project_id=project_id,
        session_id=None,
        rows=24,
        cols=80,
        cwd=None,
        command=["echo", "requested"],
    )
    assert requested.success is True
    requested_row = manager.get(requested.terminal_id)
    assert requested_row is not None
    assert created[-1] == (80, 24)
    assert (requested_row.cols, requested_row.rows) == created[-1]

    agent_terminal_uuid = uuid4()
    agent_terminal_id = str(agent_terminal_uuid)
    agent_spawn_key = f"gobby-{uuid4().hex}"
    attempt = manager.create_pending(
        agent_terminal_id,
        project_id,
        "tmux",
        "gobby",
        agent_spawn_key,
    )
    prepared = await runtime.prepare_spawn(
        TerminalSpawnRequest(
            terminal_id=agent_terminal_uuid,
            spawn_key=agent_spawn_key,
            command=["echo", "agent"],
        )
    )
    agent_result = await _promote_prepared(
        cast(SpawnRequest, SimpleNamespace(run_manager=None)),
        cast(
            ProviderSpawnPlan,
            SimpleNamespace(
                agent_run_id=str(uuid4()),
                child_session_id=str(uuid4()),
                title=None,
                auth_cli="codex",
            ),
        ),
        manager=cast(TerminalManager, manager),
        runtime=runtime,
        backend="tmux",
        terminal_id=agent_terminal_id,
        spawn_key=agent_spawn_key,
        prepared=prepared,
        attempt_generation=attempt.attempt_generation,
        attempt_started_at=attempt.attempt_started_at,
    )
    assert agent_result.success is True
    agent_row = manager.get(agent_terminal_id)
    assert agent_row is not None
    assert created[-1] == (200, 50)
    assert (agent_row.cols, agent_row.rows) == created[-1]


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
        rows: int | None = 50,
        cols: int | None = 200,
    ) -> TmuxSessionInfo:
        del command, cwd, env, rows, cols
        created.append(name)
        return TmuxSessionInfo(name=name, pane_pid=42, pane_id="%9")

    sessions.create_session = create_session
    sessions._run = AsyncMock(
        side_effect=[
            (0, "24 80", ""),
            (0, "/tmp/tmux.sock|1658|1784592177|%9", ""),
        ]
    )
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
async def test_snapshot_passes_the_requested_mode_to_capture() -> None:
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
    sessions.capture_pane = AsyncMock(return_value="\x1b[2mfaint\x1b[0m")
    sessions._run = AsyncMock(return_value=(0, "12|10000", ""))

    styled = await runtime.snapshot(make_memory_terminal(), lines=40, mode="ansi")

    assert styled.text == "\x1b[2mfaint\x1b[0m"
    assert sessions.capture_pane.await_args is not None
    assert sessions.capture_pane.await_args.kwargs == {"lines": 40, "mode": "ansi"}


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


@pytest.mark.asyncio
async def test_session_present_when_remain_on_exit_pane_is_dead() -> None:
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
    terminal = make_memory_terminal(session_name="gobby-orphan")
    sessions.has_session = AsyncMock(return_value=True)
    sessions._run = AsyncMock(return_value=(0, "1", ""))

    assert await runtime.is_live(terminal) is False
    assert await runtime.session_present(terminal) is True
    sessions.has_session.assert_awaited_once_with("gobby-orphan")


@pytest.mark.asyncio
async def test_terminate_kills_on_the_terminals_own_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External rows probe their own socket, so the kill must target it too."""
    sessions = _sessions()
    runtime = TmuxTerminalRuntime(sessions)
    terminal = replace(make_memory_terminal(session_name="ext-demo"), ownership="external")
    killed_sockets: list[str | None] = []

    async def fake_kill(
        self: TmuxSessionManager, name: str, *, missing_ok: bool = False, timeout: float = 5.0
    ) -> bool:
        killed_sockets.append(self.config.socket_path)
        return True

    monkeypatch.setattr(TmuxSessionManager, "kill_session", fake_kill)
    await runtime.terminate(terminal, grace_seconds=5.0)
    locator = terminal.locator or {}
    assert killed_sockets == [locator["socket_path"]]
    assert killed_sockets != [sessions.config.socket_path]

"""WebSocket routing and ledger contracts for raw terminal input."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import MethodType
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.servers.websocket.handlers import HandlerMixin
from gobby.servers.websocket.server import WebSocketServer
from gobby.servers.websocket.terminal_ws import TerminalWsMixin
from gobby.storage.terminals import Terminal
from gobby.terminals.runtime import Delivered, TerminalWriteError, WriteOutcome
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal, runtime_registry

TmuxResult = tuple[int, str, str]


@dataclass
class _Sessions:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def base_args(self) -> list[str]:
        return ["tmux"]

    async def _run(self, *args: str, **_kwargs: object) -> TmuxResult:
        self.calls.append(args)
        return 0, "", ""


@dataclass
class _RecordingRuntime:
    backend: Literal["tmux", "native"] = "tmux"
    effects: list[WriteOutcome | BaseException] = field(default_factory=list)
    inputs: list[bytes] = field(default_factory=list)
    pastes: list[str] = field(default_factory=list)

    async def write_input(self, terminal: Terminal, data: bytes) -> WriteOutcome:
        del terminal
        self.inputs.append(data)
        effect = self.effects.pop(0) if self.effects else Delivered()
        if isinstance(effect, BaseException):
            raise effect
        return effect

    async def write_text(self, terminal: Terminal, text: str, submit: bool) -> WriteOutcome:
        del terminal, text, submit
        raise AssertionError("terminal_input must use write_input")

    async def write_paste(self, terminal: Terminal, text: str) -> WriteOutcome:
        del terminal
        self.pastes.append(text)
        return Delivered()


class _WebSocket:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    def messages_of_type(self, message_type: str) -> list[dict[str, Any]]:
        messages = (json.loads(raw) for raw in self.sent_messages)
        return [message for message in messages if message.get("type") == message_type]


def _server(terminal: Terminal, runtime: Any) -> tuple[WebSocketServer, str]:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="user"))
    server.configure_terminals(MemoryTerminalStore(terminal), runtime_registry(runtime))
    attachment_id = "input-attachment"
    server._leases().attach(terminal.id, attachment_id=attachment_id)
    result = server._leases().take_control(terminal.id, attachment_id)
    assert result.granted
    return server, attachment_id


def _input_message(
    terminal: Terminal,
    attachment_id: str,
    *,
    seq: int,
    data: str,
) -> dict[str, Any]:
    return {
        "type": "terminal_input",
        "terminal_id": terminal.id,
        "attachment_id": attachment_id,
        "data": data,
        "client_write_seq": seq,
    }


@pytest.mark.asyncio
async def test_input_bytes_are_key_codes_and_paste_stays_bracketed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _Sessions()
    runtime = TmuxTerminalRuntime(cast(TmuxSessionManager, sessions))
    terminal = make_memory_terminal()
    server, attachment_id = _server(terminal, runtime)
    websocket = _WebSocket()

    for seq, data in enumerate(("\x04", "\x03", "\x1b[A"), start=1):
        await server._handle_terminal_input(
            websocket,
            _input_message(terminal, attachment_id, seq=seq, data=data),
        )

    encoded = bytes(
        int(part, 16)
        for call in sessions.calls
        if call and call[0] == "send-keys"
        for part in call[4:]
    )
    assert encoded == b"\x04\x03\x1b[A"

    pasted: list[str] = []

    async def query_flags(_terminal: Terminal, _target: str) -> tuple[bool, bool, bool]:
        return False, False, True

    async def capture_paste(_target: str, text: str, **_kwargs: object) -> None:
        pasted.append(text)

    monkeypatch.setattr(runtime, "_query_flags", query_flags)
    monkeypatch.setattr(
        "gobby.terminals.tmux_runtime.paste_literal_text_to_tmux_target",
        capture_paste,
    )
    payload = "paste-data" * 2_000
    await server._handle_terminal_paste(
        websocket,
        {
            "terminal_id": terminal.id,
            "attachment_id": attachment_id,
            "text": payload,
            "client_write_seq": 4,
        },
    )
    assert pasted == [f"\x1b[200~{payload}\x1b[201~"]


@pytest.mark.asyncio
async def test_single_input_handler_is_bound_and_backend_neutral() -> None:
    assert not hasattr(HandlerMixin, "_handle_terminal_input")
    terminal = make_memory_terminal()
    server, _attachment_id = _server(terminal, _RecordingRuntime())
    websocket = _WebSocket()
    # Shadow the mixin method on the instance: dispatch must bind TerminalWsMixin
    # explicitly rather than looking the handler up through the instance.
    server.__dict__["_handle_terminal_input"] = AsyncMock(
        side_effect=AssertionError("dispatch must bind TerminalWsMixin explicitly")
    )

    await server._handle_message(
        websocket,
        json.dumps(
            {
                "type": "terminal_input",
                "terminal_id": "tmux-2367e0fb25",
                "data": "\x1b[?1;2c",
            }
        ),
    )

    handler = server._dispatch_table["terminal_input"]
    assert isinstance(handler, MethodType)
    assert handler.__self__ is server
    assert handler.__func__ is TerminalWsMixin._handle_terminal_input
    assert websocket.sent_messages == []


@pytest.mark.asyncio
async def test_partial_write_reports_indeterminate_on_the_wire() -> None:
    runtime = _RecordingRuntime(
        effects=[
            TerminalWriteError(stage="partial", delivered_bytes=512),
            TerminalWriteError(stage="partial"),
        ]
    )
    terminal = make_memory_terminal()
    server, attachment_id = _server(terminal, runtime)
    websocket = _WebSocket()

    for seq in (1, 2):
        await server._handle_terminal_input(
            websocket,
            _input_message(terminal, attachment_id, seq=seq, data=f"payload-{seq}"),
        )

    outcomes = websocket.messages_of_type("terminal_write_outcome")
    assert [(item["outcome"], item["reason"]) for item in outcomes] == [
        ("indeterminate", "indeterminate_partial_delivered:512"),
        ("indeterminate", "indeterminate_backend"),
    ]
    assert server._leases().completed_write(attachment_id, 1) == (
        "indeterminate",
        "indeterminate_partial_delivered:512",
    )
    assert server._leases().completed_write(attachment_id, 2) == (
        "indeterminate",
        "indeterminate_backend",
    )


@pytest.mark.asyncio
async def test_failed_write_completes_ledger_and_admits_next_seq() -> None:
    runtime = _RecordingRuntime(
        effects=[TerminalWriteError(stage="none", delivered_bytes=0), Delivered()]
    )
    terminal = make_memory_terminal()
    server, attachment_id = _server(terminal, runtime)
    websocket = _WebSocket()
    first = _input_message(terminal, attachment_id, seq=1, data="failed")

    await server._handle_terminal_input(websocket, first)
    assert server._leases().completed_write(attachment_id, 1) == ("refused", "held")

    await server._handle_terminal_input(websocket, first)
    replay = websocket.messages_of_type("terminal_write_outcome")[-1]
    assert (replay["outcome"], replay["reason"]) == ("refused", "held")

    changed = _input_message(terminal, attachment_id, seq=1, data="different")
    await server._handle_terminal_input(websocket, changed)
    assert websocket.messages_of_type("terminal_write_outcome")[-1]["reason"] == (
        "write_seq_conflict"
    )

    await server._handle_terminal_input(
        websocket,
        _input_message(terminal, attachment_id, seq=2, data="next"),
    )
    assert websocket.messages_of_type("terminal_write_outcome")[-1]["outcome"] == "delivered"
    assert runtime.inputs == [b"failed", b"next"]


@pytest.mark.asyncio
async def test_disconnect_cancellation_closes_ledger_without_replying() -> None:
    runtime = _RecordingRuntime(effects=[asyncio.CancelledError(), Delivered()])
    terminal = make_memory_terminal()
    server, attachment_id = _server(terminal, runtime)
    websocket = _WebSocket()
    first = _input_message(terminal, attachment_id, seq=1, data="cancelled")

    with pytest.raises(asyncio.CancelledError):
        await server._handle_terminal_input(websocket, first)

    assert websocket.sent_messages == []
    assert server._leases().completed_write(attachment_id, 1) == (
        "indeterminate",
        "indeterminate_backend",
    )

    await server._handle_terminal_input(websocket, first)
    replay = websocket.messages_of_type("terminal_write_outcome")[-1]
    assert (replay["outcome"], replay["reason"]) == (
        "indeterminate",
        "indeterminate_backend",
    )

    await server._handle_terminal_input(
        websocket,
        _input_message(terminal, attachment_id, seq=1, data="changed"),
    )
    assert websocket.messages_of_type("terminal_write_outcome")[-1]["reason"] == (
        "write_seq_conflict"
    )

    await server._handle_terminal_input(
        websocket,
        _input_message(terminal, attachment_id, seq=2, data="next"),
    )
    assert websocket.messages_of_type("terminal_write_outcome")[-1]["outcome"] == "delivered"
    assert runtime.inputs == [b"cancelled", b"next"]

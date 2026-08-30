"""Proxy attach reports typed failure instead of a silent success."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import AttachLocator, TerminalManager
from gobby.terminals import TerminalRuntime, TerminalRuntimeRegistry
from tests.servers.test_terminal_ws_lease import _live_row, _send, _ws_server
from tests.servers.test_tmux_mixin import MockWebSocket

pytestmark = pytest.mark.unit

_Kind = Literal[
    "no_runtime",
    "no_opener",
    "locator_raises",
    "locator_invalid",
    "opener_raises",
]


class _LocatorRuntime:
    """Non-Mock runtime so _runtime_for does not treat it as missing."""

    backend = "native"

    def __init__(self, result: object | None = None, *, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error

    async def attach_locator(self, row: object) -> object:
        del row
        if self._error is not None:
            raise self._error
        return self._result


def _valid_locator() -> AttachLocator:
    return AttachLocator(backend="native", frame_host_epoch="epoch-1", host_terminal_id="host-web")


async def _unused_opener(_locator: AttachLocator) -> object:
    raise AssertionError("open_proxy_frame should not run")


async def _raising_opener(_locator: AttachLocator) -> object:
    raise OSError("frame host down")


def _configure(server: Any, temp_db: HubDatabase, kind: _Kind) -> None:
    registry = TerminalRuntimeRegistry()
    if kind != "no_runtime":
        if kind == "locator_raises":
            runtime: _LocatorRuntime = _LocatorRuntime(error=RuntimeError("locator boom"))
        elif kind == "locator_invalid":
            runtime = _LocatorRuntime(result={"not": "a locator"})
        else:
            runtime = _LocatorRuntime(result=_valid_locator())
        registry.register(cast(TerminalRuntime, runtime))
    server.configure_terminals(TerminalManager(temp_db), registry, MagicMock())
    if kind == "opener_raises":
        server.open_proxy_frame = _raising_opener
    elif kind in {"locator_raises", "locator_invalid"}:
        server.open_proxy_frame = _unused_opener


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code", "reason"),
    [
        ("no_runtime", "runtime_unavailable", "no terminal runtime for backend"),
        ("no_opener", "proxy_unavailable", "proxy frame opener is not available"),
        ("locator_raises", "locator_failed", "attach_locator raised"),
        (
            "locator_invalid",
            "locator_invalid",
            "attach_locator did not return an AttachLocator",
        ),
        ("opener_raises", "host_unavailable", "proxy frame handshake failed"),
    ],
)
async def test_proxy_attach_failures_are_typed_and_finalized(
    kind: _Kind,
    code: str,
    reason: str,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    terminal_id = _live_row(temp_db, sample_project)
    server = _ws_server()
    _configure(server, temp_db, kind)
    ws = MockWebSocket()
    server.clients[ws] = {"subscriptions": {"*"}}
    with caplog.at_level(logging.WARNING, logger="gobby.servers.websocket.terminal_ws"):
        await _send(
            server,
            ws,
            {
                "type": "terminal_attach",
                "request_id": "proxy-fail",
                "terminal_id": terminal_id,
                "frame_delivery": "proxy",
            },
        )
    result = ws.messages_of_type("terminal_attach_result")[-1]
    assert result["success"] is False
    assert result["code"] == code
    assert result["reason"] == reason
    assert result["terminal_id"] == terminal_id
    attachment = result["attachment_id"]
    assert attachment
    assert server.lease_registry.get(attachment) is None
    assert any(
        record.levelno == logging.WARNING
        and code in record.getMessage()
        and terminal_id in record.getMessage()
        and reason in record.getMessage()
        for record in caplog.records
    )
    await _send(
        server,
        ws,
        {
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "takeover": False,
        },
    )
    control = ws.messages_of_type("terminal_control_result")[-1]
    assert control["granted"] is False
    assert control["reason"] == "stale_attachment"

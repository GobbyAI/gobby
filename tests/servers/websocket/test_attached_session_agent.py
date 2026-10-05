"""Persona commands for attached managed terminal sessions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.handlers.session_config import _set_attached_session_agent
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.terminals.runtime import Delivered, IndeterminateWrite


@pytest.fixture(autouse=True)
def base_agent_variables() -> Iterator[None]:
    with patch(
        "gobby.workflows.state_manager.SessionVariableManager.get_variables",
        return_value={"_agent_type": "default"},
    ):
        yield


def _attached_target(backend: str = "native") -> tuple[SimpleNamespace, AsyncMock]:
    session = SimpleNamespace(id="session-1", session_type="terminal", terminal_context={})
    terminal = SimpleNamespace(id="terminal-1", backend=backend)
    session_manager = MagicMock()
    session_manager.get.return_value = session
    terminal_manager = MagicMock()
    terminal_manager.resolve_live_for_session.return_value = terminal
    coordinator = MagicMock()
    coordinator.write = AsyncMock(return_value=Delivered())
    server = SimpleNamespace(
        session_manager=session_manager,
        terminal_manager=terminal_manager,
        write_coordinator=coordinator,
        _send_error=AsyncMock(),
    )
    websocket = AsyncMock()
    return server, websocket


@pytest.mark.asyncio
async def test_persona_command_uses_native_coordinator_without_tmux_context() -> None:
    server, websocket = _attached_target()

    await _set_attached_session_agent(
        cast(SessionControlMixin, server), websocket, "session-1", "default"
    )

    request = server.write_coordinator.write.await_args.args[0]
    assert request.terminal_id == "terminal-1"
    assert request.kind == "text"
    assert request.payload == "/gobby persona default"
    assert request.submit is True
    assert json.loads(websocket.send.await_args.args[0])["type"] == "agent_changed"
    server._send_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_persona_command_fences_live_legacy_terminal() -> None:
    server, websocket = _attached_target("tmux")

    await _set_attached_session_agent(
        cast(SessionControlMixin, server), websocket, "session-1", "default"
    )

    assert server._send_error.await_args.kwargs["code"] == "UNSUPPORTED_TERMINAL_BACKEND"
    server.write_coordinator.write.assert_not_awaited()
    websocket.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_persona_command_does_not_confirm_indeterminate_write() -> None:
    server, websocket = _attached_target()
    server.write_coordinator.write.return_value = IndeterminateWrite("lost reply")

    await _set_attached_session_agent(
        cast(SessionControlMixin, server), websocket, "session-1", "default"
    )

    assert server._send_error.await_args.kwargs["code"] == "PERSONA_DISPATCH_UNCONFIRMED"
    websocket.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_attached_terminal_role_change_refused_before_write() -> None:
    server, websocket = _attached_target()
    with (
        patch(
            "gobby.servers.websocket.handlers.session_config._validate_persona_agent",
            return_value=True,
        ),
        patch(
            "gobby.workflows.state_manager.SessionVariableManager.get_variables",
            return_value={"_agent_type": "x"},
        ),
        patch(
            "gobby.storage.config_repository.ConfigRepository.read",
            return_value=SimpleNamespace(values={"default_agent": "default"}),
        ),
    ):
        await _set_attached_session_agent(
            cast(SessionControlMixin, server), websocket, "session-1", "y"
        )
    server.write_coordinator.write.assert_not_awaited()
    assert server._send_error.await_args.kwargs["code"] == "ROLE_CHANGE_REQUIRES_RELAUNCH"
    text = server._send_error.await_args.args[1]
    assert "start a new terminal session" in text
    assert "'x'" in text and "'y'" in text
    assert json.loads(websocket.send.await_args.args[0]) == {
        "type": "agent_changed",
        "target_session_id": "session-1",
        "agent_name": "x",
    }

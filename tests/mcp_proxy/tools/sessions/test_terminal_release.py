"""The release_composer valve on gobby-sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.storage.terminals import Terminal
from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead
from gobby.utils.session_context import session_context_for_test
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

_SEAT = "seat-session"


def _release_tool() -> tuple[Callable[..., Any], Terminal]:
    seat = make_memory_terminal()
    seat.session_id = _SEAT
    sessions = MagicMock()
    sessions.resolve_session_reference.side_effect = lambda reference, _project_id=None: reference
    registry = InternalToolRegistry(name="test", description="test")
    with patch(
        "gobby.mcp_proxy.tools.sessions._terminal.LocalAgentRunManager",
        return_value=MagicMock(),
    ):
        register_terminal_tools(
            registry,
            sessions,
            MagicMock(fetchone=MagicMock(return_value=None)),
            terminal_manager=MemoryTerminalStore(seat),
            terminal_runtime_registry=runtime_registry(FakeRuntime()),
        )
    tool = registry.get_tool("release_composer")
    assert tool is not None
    return tool, seat


def test_another_session_releases_a_pre_ledger_seat(composer_ledger: ComposerLedger) -> None:
    tool, seat = _release_tool()

    with session_context_for_test("assistant-session"):
        result = asyncio.run(tool(session_id=_SEAT))

    assert result == {"success": True, "session_id": _SEAT, "terminal_id": seat.id}
    assert composer_ledger.read(seat.id) == LedgerRead("empty")


def test_a_seat_cannot_release_its_own_composer(composer_ledger: ComposerLedger) -> None:
    tool, seat = _release_tool()

    with session_context_for_test(_SEAT):
        result = asyncio.run(tool(session_id=_SEAT))

    assert result["success"] is False
    assert result["error_code"] == "self_release"
    assert composer_ledger.read(seat.id) == LedgerRead("blocked", "untracked")

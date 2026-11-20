"""A non-terminal session never reaches a pane through its terminal context.

The wake and composer sites resolve a session's terminal with one rule, and that rule
accepts a row named by ``terminal_context`` only for a ``terminal`` session. The pane
routes (``_resolve_pane_io``, ``capture_output`` and the compact continuation) must give
the same answer: when the shared resolver declines, no pane is returned, even though the
context names an otherwise eligible live, unbound row of the session's project.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import _terminal
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.sessions.compact_continuation import _continuation_pane
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals.runtime import TerminalRuntime, TerminalRuntimeRegistry
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

SESSION_ID = "00000000-0000-4000-8000-0000000026a1"
TERMINAL_ID = "00000000-0000-4000-8000-0000000026a2"


class _Sessions:
    """Session manager stand-in that serves one synthetic session row."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def get(self, session_id: str) -> Any:
        return self._session if session_id == self._session.id else None


class _NoRows:
    """Database stand-in for tool registration: every lookup finds no row."""

    def fetchone(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchall(self, *_args: object, **_kwargs: object) -> list[Any]:
        return []


def _web_chat_session_naming_a_live_row() -> tuple[Any, MemoryTerminalStore, FakeRuntime]:
    """A web_chat session whose context names a live, unbound, same-project row."""
    terminal = make_memory_terminal(terminal_id=TERMINAL_ID)
    assert terminal.session_id is None
    assert terminal.agent_run_id is None
    session = SimpleNamespace(
        id=SESSION_ID,
        project_id=terminal.project_id,
        session_type="web_chat",
        terminal_context={"gobby_terminal_id": terminal.id},
        transcript_path=None,
    )
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(snapshot_text="pane text a web_chat session must not read")
    # The shared resolver is the single answer; for this session it declines.
    assert store.resolve_live_for_session(session) is None
    return session, store, runtime


def _registry(runtime: FakeRuntime) -> TerminalRuntimeRegistry:
    registry = TerminalRuntimeRegistry()
    registry.register(cast(TerminalRuntime, runtime))
    return registry


def test_resolve_pane_io_gives_a_non_terminal_session_no_context_pane() -> None:
    session, store, runtime = _web_chat_session_naming_a_live_row()

    pane, error = _terminal._resolve_pane_io(
        SESSION_ID,
        cast(SessionManager, _Sessions(session)),
        cast(LocalAgentRunManager, None),
        terminal_manager=store,
        terminal_runtime_registry=_registry(runtime),
    )

    assert pane is None, f"web_chat session got a pane over {getattr(pane, 'target', None)}"
    assert error == f"No live managed terminal for session {SESSION_ID}"


def test_capture_output_does_not_read_a_pane_for_a_non_terminal_session() -> None:
    session, store, runtime = _web_chat_session_naming_a_live_row()
    tools = InternalToolRegistry(name="test", description="test")
    register_terminal_tools(
        tools,
        cast(SessionManager, _Sessions(session)),
        cast(HubDatabase, _NoRows()),
        terminal_manager=store,
        terminal_runtime_registry=_registry(runtime),
    )
    metadata = tools.get_tool_metadata("capture_output")
    assert metadata is not None
    assert metadata.func is not None

    result = asyncio.run(metadata.func(session_id=SESSION_ID, lines=20))

    assert runtime.snapshot_modes == [], f"pane was snapshotted: {result!r}"
    assert result["success"] is False
    assert result["error_code"] == "no_live_pane_or_transcript"


def test_compact_continuation_gives_a_non_terminal_session_no_context_pane() -> None:
    session, store, runtime = _web_chat_session_naming_a_live_row()

    pane = _continuation_pane(session, SESSION_ID, store, _registry(runtime))

    assert pane is None, f"web_chat session got a pane over {getattr(pane, 'target', None)}"

"""Explicit session refs on async hook-dispatched MCP calls resolve before seeding.

Drives ``ChatLifecycleMixin._dispatch_mcp_calls``, the web-chat entry point, on a
minimal host. The host's tool proxy is a typed double that carries a real
SessionManager on the isolated test hub, so ``#N`` refs resolve for real. The
double's ``call_tool`` stands in for the tool body and records the session
context it observes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.servers.websocket.chat._lifecycle import ChatLifecycleMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import get_current_session_id
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

UNKNOWN_SESSION_REF = "#999999"


class _RecordingProxy:
    """Tool-proxy double: a real session manager, and a call_tool that records its seeded session."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.session_manager = session_manager
        self.calls: list[tuple[str, str | None]] = []

    async def call_tool(
        self,
        _server: str,
        tool: str,
        _arguments: dict[str, Any],
        **_options: Any,
    ) -> dict[str, Any]:
        self.calls.append((tool, get_current_session_id()))
        return {"success": True}


class _WebChatHost(ChatLifecycleMixin):
    """Minimal web-chat host: only the tool proxy getter the dispatch path reads."""

    def __init__(self, proxy: _RecordingProxy) -> None:
        self.tool_proxy_getter = lambda: proxy


@dataclass(frozen=True)
class _ChatWorld:
    proxy: _RecordingProxy
    host: _WebChatHost
    session_id: str
    seq_num: int
    project_id: str
    project_name: str
    project_path: str


@pytest.fixture
def chat_world(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ChatWorld:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    sessions = SessionManager(temp_db)
    caller = sessions.register(
        external_id="chat-caller",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=isolated.project.id,
    )
    stored = sessions.get(caller.id)
    assert stored is not None and stored.seq_num is not None
    # The fixture itself must resolve the ref the tests send, so a red result is the seeding.
    assert sessions.resolve_session_reference(f"#{stored.seq_num}", isolated.project.id) == (
        caller.id
    )
    proxy = _RecordingProxy(sessions)
    return _ChatWorld(
        proxy=proxy,
        host=_WebChatHost(proxy),
        session_id=caller.id,
        seq_num=stored.seq_num,
        project_id=isolated.project.id,
        project_name=isolated.project.name,
        project_path=isolated.root_path,
    )


@contextmanager
def _project_scope(world: _ChatWorld) -> Iterator[None]:
    """Make the caller's project the current project, as the daemon does per request."""
    token = set_project_context(
        {"id": world.project_id, "name": world.project_name, "project_path": world.project_path}
    )
    try:
        yield
    finally:
        reset_project_context(token)


def _event(world: _ChatWorld) -> HookEvent:
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="chat-external-1",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={},
    )
    event.project_id = world.project_id
    return event


def _explicit_call(session_ref: str) -> dict[str, Any]:
    """An mcp_call effect that addresses a session explicitly and captures its result."""
    return {
        "server": "synthetic-server",
        "tool": "echo_session",
        "arguments": {"session_id": session_ref},
        "inject_result": True,
    }


@pytest.mark.asyncio
async def test_explicit_number_ref_seeds_the_resolved_session_uuid(
    chat_world: _ChatWorld,
) -> None:
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_explicit_call(f"#{chat_world.seq_num}")], _event(chat_world)
        )

    assert chat_world.proxy.calls == [("echo_session", chat_world.session_id)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_unresolvable_number_ref_fails_the_call_without_running_the_tool(
    chat_world: _ChatWorld,
) -> None:
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_explicit_call(UNKNOWN_SESSION_REF)], _event(chat_world)
        )

    assert chat_world.proxy.calls == []
    assert results[0]["success"] is False

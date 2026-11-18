"""Session refs on async hook-dispatched MCP calls resolve before the tool runs.

Drives ``ChatLifecycleMixin._dispatch_mcp_calls``, the web-chat entry point, on a
minimal host. The host's tool proxy is a typed double that carries a real
SessionManager on the isolated test hub, so ``#N`` refs resolve for real. The
double's ``call_tool`` stands in for the tool body and records what it received:
the session argument, the seeded session context and the session the proxy got.
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


@dataclass(frozen=True)
class _Received:
    """What one call's tool body and the proxy saw."""

    tool: str
    argument_session_id: str | None
    context_session_id: str | None
    proxy_session_id: str | None


class _RecordingProxy:
    """Tool-proxy double: a real session manager, and a call_tool that records each call."""

    def __init__(self, session_manager: SessionManager) -> None:
        self.session_manager = session_manager
        self.calls: list[_Received] = []

    async def call_tool(
        self,
        _server: str,
        tool: str,
        arguments: dict[str, Any],
        **options: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            _Received(
                tool=tool,
                argument_session_id=arguments.get("session_id"),
                context_session_id=get_current_session_id(),
                proxy_session_id=options.get("session_id"),
            )
        )
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


def _event(world: _ChatWorld, *, platform_session_id: str | None = None) -> HookEvent:
    """A web-chat event in the caller's project, carrying an injected platform session id."""
    metadata: dict[str, Any] = {}
    if platform_session_id is not None:
        metadata["_platform_session_id"] = platform_session_id
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="chat-external-1",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata=metadata,
    )
    event.project_id = world.project_id
    return event


def _call(arguments: dict[str, Any]) -> dict[str, Any]:
    """An mcp_call effect that captures its result, with the given arguments."""
    return {
        "server": "synthetic-server",
        "tool": "echo_session",
        "arguments": arguments,
        "inject_result": True,
    }


def _resolved(world: _ChatWorld) -> _Received:
    """What the tool sees when the caller's ref resolves to its session UUID."""
    return _Received("echo_session", world.session_id, world.session_id, world.session_id)


@pytest.mark.asyncio
async def test_explicit_number_ref_seeds_the_resolved_session_uuid(
    chat_world: _ChatWorld,
) -> None:
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": f"#{chat_world.seq_num}"})], _event(chat_world)
        )

    assert chat_world.proxy.calls == [_resolved(chat_world)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_explicit_uuid_ref_seeds_its_session(chat_world: _ChatWorld) -> None:
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": chat_world.session_id})], _event(chat_world)
        )

    assert chat_world.proxy.calls == [_resolved(chat_world)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_unresolvable_number_ref_fails_the_call_without_running_the_tool(
    chat_world: _ChatWorld,
) -> None:
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": UNKNOWN_SESSION_REF})], _event(chat_world)
        )

    assert chat_world.proxy.calls == []
    assert results[0]["success"] is False


@pytest.mark.asyncio
async def test_ambient_number_ref_that_resolves_seeds_the_resolved_session_uuid(
    chat_world: _ChatWorld,
) -> None:
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({})],
            _event(chat_world, platform_session_id=f"#{chat_world.seq_num}"),
        )

    assert chat_world.proxy.calls == [_resolved(chat_world)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_unresolvable_ambient_ref_runs_the_tool_as_the_sync_dispatcher_does(
    chat_world: _ChatWorld,
) -> None:
    """An injected id that does not resolve is not a failure, unlike an explicit ref.

    The synchronous dispatcher runs the tool with the id as given, seeds no session
    from it, and passes no session to the proxy. This path does the same.
    """
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({})],
            _event(chat_world, platform_session_id=UNKNOWN_SESSION_REF),
        )

    assert chat_world.proxy.calls == [
        _Received("echo_session", UNKNOWN_SESSION_REF, None, None),
    ]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_number_ref_on_an_event_without_a_project_resolves_in_the_ambient_project(
    chat_world: _ChatWorld,
) -> None:
    event = _event(chat_world)
    event.project_id = None
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": f"#{chat_world.seq_num}"})], event
        )

    assert chat_world.proxy.calls == [_resolved(chat_world)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_empty_session_id_passes_through_unseeded(chat_world: _ChatWorld) -> None:
    """An empty-string session_id is not replaced by the injected id.

    The tool receives the empty string, and nothing is resolved or seeded from it.
    """
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": ""})],
            _event(chat_world, platform_session_id=chat_world.session_id),
        )

    assert chat_world.proxy.calls == [_Received("echo_session", "", None, None)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("template", [" #{seq}", "#{seq} ", "\t{seq}\n"])
async def test_padded_number_ref_resolves_like_the_sync_dispatcher(
    chat_world: _ChatWorld, template: str
) -> None:
    """The sync dispatcher strips a ref before it resolves it, so a padded ref resolves too."""
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": template.format(seq=chat_world.seq_num)})],
            _event(chat_world),
        )

    assert chat_world.proxy.calls == [_resolved(chat_world)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
async def test_blank_session_id_passes_through_unseeded_like_an_empty_one(
    chat_world: _ChatWorld,
) -> None:
    """A whitespace-only session_id strips to empty: the tool gets it as given, unseeded."""
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": "   "})],
            _event(chat_world, platform_session_id=chat_world.session_id),
        )

    assert chat_world.proxy.calls == [_Received("echo_session", "   ", None, None)]
    assert results[0]["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [7, ["#1"], {"ref": "#1"}, True])
async def test_truthy_non_string_session_id_fails_the_call_without_running_the_tool(
    chat_world: _ChatWorld, value: Any
) -> None:
    """The sync dispatcher fails a truthy non-string session_id before the tool runs."""
    with _project_scope(chat_world):
        results = await chat_world.host._dispatch_mcp_calls(
            [_call({"session_id": value})], _event(chat_world)
        )

    assert chat_world.proxy.calls == []
    assert results[0]["success"] is False

"""MCP send_keys and capture_output are backend-neutral (plan 2.4.6)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._terminal import register_terminal_tools
from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import Delivered, IndeterminateWrite
from gobby.terminals.write_coordinator import UnresolvedWriteStore, WriteCoordinator
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


def _registered_send_keys(
    temp_db: HubDatabase,
    *,
    context_only: bool = False,
) -> tuple[Callable[..., Any], FakeRuntime, MemoryTerminalStore, WriteCoordinator]:
    terminal = make_memory_terminal(backend="native")
    terminal.session_id = None if context_only else "target-session"
    if context_only:
        terminal.project_id = "project-1"
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native")
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    caller = MagicMock(id="caller-session", project_id="project-1", agent_run_id=None)
    target = MagicMock(
        id="target-session",
        project_id="project-1",
        session_type="terminal",
        terminal_context={"gobby_terminal_id": terminal.id} if context_only else None,
    )
    session_manager = MagicMock()
    session_manager.resolve_session_reference.side_effect = lambda ref, project_id=None: ref
    session_manager.get.side_effect = {
        "caller-session": caller,
        "target-session": target,
    }.get
    tools = InternalToolRegistry(name="gobby-sessions", description="sessions")
    register_terminal_tools(
        tools,
        session_manager,
        temp_db,
        terminal_manager=store,
        write_coordinator=coordinator,
    )
    send = tools.get_tool("send_keys")
    assert send is not None
    return send, runtime, store, coordinator


@pytest.mark.asyncio
async def test_send_keys_uses_unbound_native_context_id(temp_db: HubDatabase) -> None:
    send, runtime, _store, _coordinator = _registered_send_keys(temp_db, context_only=True)

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        result = await send(session_id="target-session", keys="hello\n", literal=True)

    assert result["success"] is True
    assert runtime.write_log == [("text", "hello\n")]


@pytest.mark.asyncio
async def test_send_and_capture_are_backend_neutral(temp_db: HubDatabase) -> None:
    terminal = make_memory_terminal(backend="native")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native")
    runtime.snapshot_text = "pane"
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    coordinator = WriteCoordinator(
        cast(UnresolvedWriteStore, store),
        runtime_registry(runtime),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
    )
    tools = InternalToolRegistry(name="gobby-sessions", description="sessions")
    session_manager = MagicMock()
    register_terminal_tools(
        tools,
        session_manager,
        temp_db,
        terminal_manager=store,
        terminal_runtime_registry=registry,
        write_coordinator=coordinator,
    )
    send = tools.get_tool("send_keys")
    capture = tools.get_tool("capture_output")
    assert send is not None
    assert capture is not None
    assert runtime.backend == "native"


@pytest.mark.asyncio
async def test_send_keys_uses_daemon_origin_and_idempotency(temp_db: HubDatabase) -> None:
    send, runtime, store, coordinator = _registered_send_keys(temp_db)
    terminal = next(iter(store.rows.values()))
    await coordinator.lease_registry.attach(terminal.id, attachment_id="operator")
    await coordinator.lease_registry.take_control(terminal.id, "operator")
    coordinator_write = AsyncMock(wraps=coordinator.write)

    with (
        patch.object(coordinator, "write", coordinator_write),
        patch(
            "gobby.utils.session_context.get_current_session_id",
            return_value="caller-session",
        ),
    ):
        result = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="request-1",
        )
        replay = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="request-1",
        )

    assert result == {"success": True, "idempotency_key": "request-1"}
    assert replay == result
    assert coordinator_write.await_args is not None
    assert coordinator_write.await_args.args[0].origin == "daemon"
    assert runtime.write_log == [("text", "hello"), ("text", "hello")]


@pytest.mark.asyncio
async def test_send_keys_idempotency_conflict(temp_db: HubDatabase) -> None:
    send, runtime, store, _coordinator = _registered_send_keys(temp_db)
    runtime.outcome = IndeterminateWrite(detail="reply lost")

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        first = await send(
            session_id="target-session",
            keys="hello",
            idempotency_key="request-1",
        )
        conflict = await send(
            session_id="target-session",
            keys="different",
            idempotency_key="request-1",
        )

    assert first["indeterminate"] is True
    assert conflict == {
        "success": False,
        "error": "idempotency key was already used with a different payload",
        "error_code": "idempotency_conflict",
        "idempotency_key": "request-1",
    }
    assert runtime.write_log == [("text", "hello")]
    terminal = next(iter(store.rows.values()))
    entry = terminal.unresolved_writes["mcp-send-keys:target-session:request-1"]
    assert re.fullmatch(r"[0-9a-f]{64}", entry["payload_fingerprint"])


@pytest.mark.asyncio
async def test_send_keys_idempotency_key_contract(temp_db: HubDatabase) -> None:
    send, runtime, _store, _coordinator = _registered_send_keys(temp_db)

    with patch(
        "gobby.utils.session_context.get_current_session_id",
        return_value="caller-session",
    ):
        runtime.outcome = IndeterminateWrite(detail="reply lost")
        first = await send(
            session_id="target-session",
            keys="maybe",
            idempotency_key="explicit",
        )
        replay = await send(
            session_id="target-session",
            keys="maybe",
            idempotency_key="explicit",
        )
        assert len(runtime.write_log) == 1

        runtime.outcome = Delivered()
        delivered_once = await send(
            session_id="target-session",
            keys="delivered",
            idempotency_key="reusable",
        )
        delivered_twice = await send(
            session_id="target-session",
            keys="delivered",
            idempotency_key="reusable",
        )
        malformed = await send(
            session_id="target-session",
            keys="blocked",
            idempotency_key="contains whitespace",
        )

        runtime.outcome = IndeterminateWrite(detail="reply lost")
        minted = await send(session_id="target-session", keys="minted")
        minted_replay = await send(
            session_id="target-session",
            keys="minted",
            idempotency_key=minted["idempotency_key"],
        )

    assert first["indeterminate"] is True
    assert replay["indeterminate"] is True
    assert first["idempotency_key"] == replay["idempotency_key"] == "explicit"
    assert delivered_once == {"success": True, "idempotency_key": "reusable"}
    assert delivered_twice == delivered_once
    assert malformed["error_code"] == "invalid_idempotency_key"
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", minted["idempotency_key"])
    assert minted_replay["indeterminate"] is True
    assert minted_replay["idempotency_key"] == minted["idempotency_key"]
    assert runtime.write_log == [
        ("text", "maybe"),
        ("text", "delivered"),
        ("text", "delivered"),
        ("text", "minted"),
    ]

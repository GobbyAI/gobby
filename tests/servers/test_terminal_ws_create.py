"""Acceptance 2.5.11: web terminal_create uses the row-owning spawn primitive."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.terminals.runtime import TerminalRuntime
from gobby.terminals.web_spawn import WebSpawnResult, spawn_web_terminal
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _manager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_web_create_uses_row_owning_primitive(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    runtime = MagicMock()
    runtime.backend = "tmux"
    runtime.prepare_spawn = AsyncMock(
        return_value=MagicMock(
            stored_locator={
                "socket_path": "/tmp/tmux/default",
                "server_pid": 1,
                "server_start_time": 2,
                "pane_id": "%1",
            },
            locator_key="tmux:/tmp/tmux/default:1:2:%1",
            process=None,
            acknowledge_persist=MagicMock(),
        )
    )
    runtime.commit_spawn = AsyncMock(
        return_value=MagicMock(locator=MagicMock(frame_host_epoch=None))
    )
    runtime.terminate = AsyncMock()
    result = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert result.success is True
    row = manager.get(result.terminal_id)
    assert row is not None
    assert row.ownership == "gobby"
    assert row.state == "live"
    listing = manager.list_by_project(sample_project["id"])
    assert row.id in {item.id for item in listing}

    runtime.prepare_spawn = AsyncMock(side_effect=Exception("backend boom"))
    failed = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert failed.success is False
    failed_row = manager.get(failed.terminal_id)
    assert failed_row is not None
    assert failed_row.state == "exited"


@pytest.mark.asyncio
async def test_native_create_saturates_then_expires(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import ast
    from pathlib import Path
    from uuid import UUID

    from gobby.terminals.host_client import HostCommandError
    from gobby.terminals.runtime import PreparedSpawn, TerminalHandle, TerminalSpawnRequest

    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    prepares: list[str] = []
    entitlements = 0
    ceiling = 4

    class SaturatingRuntime:
        backend = "native"

        async def reserve_observer(self, terminal_id: UUID) -> dict[str, str]:
            nonlocal entitlements
            if entitlements >= ceiling:
                raise HostCommandError("capacity")
            entitlements += 1
            return {
                "reservation_id": f"rsv-{terminal_id}",
                "reserve_key": str(terminal_id),
            }

        async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
            prepares.append(str(request.terminal_id))
            prepared = MagicMock()
            prepared.stored_locator = {"host_terminal_id": f"ht-{request.terminal_id}"}
            prepared.locator_key = f"native:epoch:ht-{request.terminal_id}"
            prepared.process = None
            prepared.host_terminal_id = f"ht-{request.terminal_id}"
            prepared.acknowledge_persist = MagicMock()
            prepared.acknowledge_observer = MagicMock()
            return prepared

        async def commit_spawn(self, prepared: PreparedSpawn) -> TerminalHandle:
            del prepared
            return MagicMock(locator=MagicMock(frame_host_epoch="epoch-1"))

        async def terminate(self, terminal: object, grace: float) -> None:
            del terminal, grace
            nonlocal entitlements
            entitlements = max(0, entitlements - 1)

    runtime = cast(TerminalRuntime, SaturatingRuntime())
    held: list[str] = []
    for _ in range(ceiling):
        result = await spawn_web_terminal(
            manager=manager,
            runtime=runtime,
            project_id=sample_project["id"],
            session_id=None,
            rows=24,
            cols=80,
            cwd="/tmp",
            command=["zsh"],
        )
        assert result.success is True
        held.append(result.terminal_id)
    overflow = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert overflow.success is False
    assert "capacity" in (overflow.error or "") or "reserve" in (overflow.error or "")
    assert str(overflow.terminal_id) not in prepares
    entitlements = max(0, entitlements - 1)
    recovered = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert recovered.success is True

    root = Path(__file__).resolve().parents[2]
    scan_paths = [
        root / "src/gobby/runner_broadcasting.py",
        root / "src/gobby/servers/websocket/terminal_ws.py",
        root / "src/gobby/mcp_proxy/tools/spawn_agent/_factory.py",
        root / "src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py",
        root / "src/gobby/dispatch/spawn.py",
        root / "src/gobby/scheduler/executor.py",
    ]
    allowed = {
        root / "src/gobby/agents/spawn_executor.py",
        root / "src/gobby/terminals/web_spawn.py",
    }
    for path in scan_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "reserve_observer"
        ]
        assert hits == [], f"reserve_observer at spawn ingress {path}: {hits}"
    for path in allowed:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "reserve_observer"
        ]
        assert hits, f"expected reserve_observer in primitive {path}"


@pytest.mark.asyncio
async def test_create_without_a_project_lands_in_global_and_names_the_failure(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker's machine-wide view sends no project; a refusal carries its reason (#21198)."""
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    server.terminal_manager = _manager(temp_db)
    runtime = MagicMock()
    runtime.backend = "tmux"
    server.terminal_runtime_registry = MagicMock(resolve=MagicMock(return_value=runtime))
    server.terminal_config = MagicMock(default_backend="tmux")
    spawn = AsyncMock(
        side_effect=[
            WebSpawnResult(False, "t-1", "backend boom"),
            WebSpawnResult(True, "t-2"),
        ]
    )
    monkeypatch.setattr("gobby.terminals.web_spawn.spawn_web_terminal", spawn)
    ws = MockWebSocket()
    request = {"type": "terminal_create", "rows": 24, "cols": 80, "command": ["zsh"]}

    await server._handle_terminal_create(ws, {**request, "request_id": "c-1"})
    await server._handle_terminal_create(ws, {**request, "request_id": "c-2"})

    assert [call.kwargs["project_id"] for call in spawn.await_args_list] == [
        GLOBAL_PROJECT_ID,
        GLOBAL_PROJECT_ID,
    ]
    failed, created = (json.loads(message) for message in ws.sent_messages)
    assert failed == {
        "type": "terminal_create_result",
        "request_id": "c-1",
        "success": False,
        "terminal_id": "t-1",
        "backend": "tmux",
        "reason": "backend boom",
    }
    assert (created["success"], created["terminal_id"], created["reason"]) == (True, "t-2", None)

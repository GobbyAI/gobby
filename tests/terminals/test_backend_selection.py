"""Backend selection under the tmux default (herdr-foundation-landing leaf 1.3)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import yaml

from gobby.agents.spawn_models import resolve_terminal_backend
from gobby.config.app import DaemonConfig
from gobby.config.terminals import TerminalConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals.discovery import seed_external_terminal
from gobby.terminals.host_client import HostCommandError, HostUnavailableError
from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
from gobby.terminals.runtime import PreparedSpawn, TerminalRuntime, TerminalSpawnRequest
from gobby.terminals.web_spawn import spawn_web_terminal
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _manager

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_CONFIG_YAML = _REPO / "src" / "gobby" / "install" / "shared" / "config" / "config.yaml"
_GUIDE = _REPO / "docs" / "guides" / "gterminal-development-guide.md"
_TERMINAL_WS = _REPO / "src" / "gobby" / "servers" / "websocket" / "terminal_ws.py"


@pytest.mark.asyncio
async def test_explicit_and_external_selection_under_tmux_default(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = DaemonConfig()
    assert TerminalConfig().default_backend == "tmux"
    assert daemon.terminals.default_backend == "tmux"
    loaded = yaml.safe_load(_CONFIG_YAML.read_text(encoding="utf-8"))
    assert loaded["terminals"]["default_backend"] == "tmux"
    guide = _GUIDE.read_text(encoding="utf-8")
    assert "## Backend status" in guide
    assert "`tmux` is the default backend" in guide
    assert "`host_unavailable`" in guide

    assert resolve_terminal_backend(None, daemon) == "tmux"
    assert resolve_terminal_backend(None, None) == "tmux"
    assert resolve_terminal_backend("tmux", daemon) == "tmux"
    assert resolve_terminal_backend("native", daemon) == "native"
    opted_in = DaemonConfig.model_validate({"terminals": {"default_backend": "native"}})
    assert resolve_terminal_backend(None, opted_in) == "native"
    with pytest.raises(ValueError, match="invalid terminal_backend"):
        resolve_terminal_backend("ssh", daemon)

    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    external = seed_external_terminal(
        manager,
        project_id=sample_project["id"],
        session_id=None,
        terminal_context={
            "tmux_socket_path": "/private/tmp/tmux-501/default",
            "tmux_pane": "%12",
            "tmux_session": "ext",
            "tmux_window": "@1",
            "tmux_server_pid": 1658,
            "tmux_server_start_time": 1784592177,
        },
        generation={
            "socket_path": "/private/tmp/tmux-501/default",
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "pane_id": "%12",
        },
    )
    assert external is not None
    assert external.ownership == "external"
    assert external.backend == "tmux"

    # An explicit native request with no gterm host is refused before fork with the
    # typed host_unavailable code; the pending row fails and no tmux row appears.
    assert issubclass(HostUnavailableError, HostCommandError)
    no_host = NativeTerminalRuntime(HostManagerControl(SimpleNamespace(_client=None)))
    refused = await spawn_web_terminal(
        manager=manager,
        runtime=no_host,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert refused.success is False
    assert refused.error == "host_unavailable"
    refused_row = manager.get(refused.terminal_id)
    assert refused_row is not None
    assert refused_row.backend == "native"
    assert refused_row.state == "exited"
    open_rows, _more = manager.list_page(
        [sample_project["id"]], states=("pending", "live"), limit=10
    )
    assert [row.id for row in open_rows] == [external.id]

    prepares: list[str] = []
    entitlements = 0
    max_attachments_total = 8
    ceiling = max_attachments_total - 4

    class SaturatingNativeRuntime:
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

        async def commit_spawn(self, prepared: PreparedSpawn) -> Any:
            del prepared
            return MagicMock(locator=MagicMock(frame_host_epoch="epoch-1"))

        async def terminate(self, terminal: object, grace: float) -> None:
            del terminal, grace
            nonlocal entitlements
            entitlements = max(0, entitlements - 1)

        async def bind_observer(self, prepared: PreparedSpawn, reservation_id: str) -> None:
            del prepared, reservation_id

    runtime = cast(TerminalRuntime, SaturatingNativeRuntime())
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
    assert "capacity" in (overflow.error or "")
    assert str(overflow.terminal_id) not in prepares

    tree = ast.parse(_TERMINAL_WS.read_text(encoding="utf-8"))
    create_fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name == "_handle_terminal_create"
                ):
                    create_fn = item
    assert create_fn is not None
    calls = [
        n.func.id
        for n in ast.walk(create_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "spawn_web_terminal" in calls
    bind = [n.attr for n in ast.walk(create_fn) if isinstance(n, ast.Attribute)]
    assert "reserve_observer" not in bind
    assert "prepare_spawn" not in bind
    literals = {n.value for n in ast.walk(create_fn) if isinstance(n, ast.Constant)}
    assert "native" not in literals
    assert "tmux" not in literals

    tmux_runtime = MagicMock()
    tmux_runtime.backend = "tmux"
    tmux_runtime.prepare_spawn = AsyncMock(
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
    tmux_runtime.commit_spawn = AsyncMock(
        return_value=MagicMock(locator=MagicMock(frame_host_epoch=None))
    )
    explicit = await spawn_web_terminal(
        manager=manager,
        runtime=cast(TerminalRuntime, tmux_runtime),
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert explicit.success is True
    row = manager.get(explicit.terminal_id)
    assert row is not None
    assert row.backend == "tmux"
    assert row.ownership == "gobby"

"""Composition-root identity for terminal services (plan 2.2.7)."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.app_context import ServiceContainer
from gobby.runner import GobbyRunner
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.terminals.fakes import FakeRuntime

pytestmark = pytest.mark.unit


def test_single_lease_registry_is_injected() -> None:
    from gobby.config.terminals import TerminalConfig
    from gobby.storage.terminals import TerminalManager
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.leases import TerminalLeaseRegistry
    from gobby.terminals.services import TerminalServices
    from gobby.terminals.write_coordinator import WriteCoordinator

    annotations = GobbyRunner.__annotations__
    assert "terminal_manager" in annotations
    assert "terminal_runtime_registry" in annotations
    assert "terminal_config" in annotations
    assert "frame_client" in annotations
    assert "terminal_services" in annotations
    assert "lease_registry" in annotations

    container_names = {item.name for item in fields(ServiceContainer)}
    assert "terminal_manager" in container_names
    assert "terminal_runtime_registry" in container_names
    assert "terminal_config" in container_names
    assert "frame_client" in container_names
    assert "terminal_services" in container_names
    assert "write_coordinator" in container_names
    assert "lease_registry" in container_names

    manager = MagicMock(spec=TerminalManager)
    registry = TerminalRuntimeRegistry()
    registry.register(FakeRuntime(backend="tmux"))
    leases = TerminalLeaseRegistry(daemon_epoch="test-epoch")
    coordinator = WriteCoordinator(manager, registry, lease_registry=leases)
    config = TerminalConfig()

    database = MagicMock(spec=HubDatabase)
    session_manager = MagicMock(spec=SessionManager)
    task_manager = MagicMock(spec=LocalTaskManager)
    services = ServiceContainer(
        database=database,
        session_manager=session_manager,
        task_manager=task_manager,
        terminal_manager=manager,
        terminal_runtime_registry=registry,
        terminal_config=config,
        terminal_services=TerminalServices(
            manager=manager,
            registry=registry,
            coordinator=coordinator,
        ),
        write_coordinator=coordinator,
        lease_registry=leases,
    )
    assert services.terminal_manager is manager
    assert services.terminal_runtime_registry is registry
    assert services.terminal_config is config

    ws_config = MagicMock(spec=WebSocketConfig)
    ws_config.host = "localhost"
    ws_config.port = 60888
    ws_config.ping_interval = 30
    ws_config.ping_timeout = 10
    ws_config.max_message_size = 1024
    server = WebSocketServer(ws_config, MagicMock(), AsyncMock(return_value="test-user"))
    server.configure_terminals(
        manager,
        registry,
        config,
        terminal_services=services.terminal_services,
        lease_registry=leases,
        write_coordinator=coordinator,
    )
    assert server.terminal_manager is manager
    assert server.terminal_services is services.terminal_services
    assert server.terminal_runtime_registry is registry
    assert server.terminal_config is config
    assert server.terminal_manager is services.terminal_manager
    assert server.terminal_runtime_registry is services.terminal_runtime_registry
    assert server.terminal_config is services.terminal_config
    assert server.lease_registry is services.lease_registry
    assert server.write_coordinator is services.write_coordinator
    assert coordinator.lease_registry is leases
    assert not hasattr(coordinator, "_leases")
    assert not hasattr(coordinator, "_locks")
    assert not hasattr(coordinator, "grant_lease")
    assert not hasattr(coordinator, "takeover_lease")


def test_proxy_frame_opener_is_bound_on_the_websocket_server(tmp_path: Path) -> None:
    from gobby.runner_init.servers import _bind_proxy_frame_opener
    from gobby.storage.terminals import AttachLocator
    from gobby.terminals.host_protocol import FRAMES_SOCKET_NAME

    ws_config = MagicMock(spec=WebSocketConfig)
    ws_config.host = "localhost"
    ws_config.port = 60888
    ws_config.ping_interval = 30
    ws_config.ping_timeout = 10
    ws_config.max_message_size = 1024
    server = WebSocketServer(ws_config, MagicMock(), AsyncMock(return_value="test-user"))
    host = MagicMock()
    host.socket_dir = tmp_path
    runner = MagicMock()
    runner.websocket_server = server
    runner.terminal_host_manager = host
    _bind_proxy_frame_opener(runner)
    assert callable(server.open_proxy_frame)
    locator = AttachLocator(backend="native", frame_host_epoch="epoch")
    assert locator.host_socket is None
    expected = str(tmp_path / FRAMES_SOCKET_NAME)
    assert expected.endswith(FRAMES_SOCKET_NAME)


def test_orchestration_builds_terminal_services_once() -> None:
    """One runner-owned instance feeds the monitor, the container, and every caller."""
    from gobby.runner_init import orchestration, terminal_wiring

    orchestration_source = Path(orchestration.__file__).read_text(encoding="utf-8")
    wiring_source = Path(terminal_wiring.__file__).read_text(encoding="utf-8")
    assert orchestration_source.count("init_terminal_wiring(runner, config)") == 1
    assert orchestration_source.count("TerminalServices(") == 0
    assert wiring_source.count("TerminalServices(") == 1
    assert "runner.terminal_services = TerminalServices(" in wiring_source
    assert "terminal_services=runner.terminal_services," in orchestration_source


def test_composition_roots_give_the_coordinator_the_registry() -> None:
    """Neither root may bind one runtime; the coordinator resolves per terminal."""
    from gobby.agents import lifecycle_monitor
    from gobby.runner_init import terminal_wiring

    wiring_source = Path(terminal_wiring.__file__).read_text(encoding="utf-8")
    flattened = " ".join(wiring_source.split())
    assert "lease_registry=runner.lease_registry" in flattened
    # The single-runtime resolve is what broke every write to a native terminal.
    assert 'resolve("tmux")' not in wiring_source

    monitor_source = Path(lifecycle_monitor.__file__).read_text(encoding="utf-8")
    assert "build_terminal_services(" in monitor_source


def test_wiring_sweeps_orphans_before_accepting_writes() -> None:
    from gobby.runner_init import terminal_wiring

    source = Path(terminal_wiring.__file__).read_text(encoding="utf-8")
    assert source.count("TerminalLeaseRegistry()") == 1
    assert source.count("clear_orphaned_attachment_writes(") == 1
    manager_built = source.index("runner.terminal_manager = TerminalManager(")
    registry_built = source.index("runner.lease_registry = TerminalLeaseRegistry()")
    swept = source.index("runner.terminal_manager.clear_orphaned_attachment_writes(")
    coordinator_built = source.index("runner.write_coordinator = WriteCoordinator(")
    assert manager_built < registry_built < swept < coordinator_built


def test_orchestration_gives_the_wake_dispatcher_its_terminal_lookup() -> None:
    """Without the row lookup, every interactive wake falls back to a tmux pane."""
    from gobby.runner_init import terminal_wiring

    source = Path(terminal_wiring.__file__).read_text(encoding="utf-8")
    flattened = " ".join(source.split())
    assert "runner.wake_dispatcher.set_terminal_manager(runner.terminal_manager)" in flattened


def test_wiring_hands_one_workspace_manager_to_both_servers() -> None:
    """The in-flight spawn guard lives on one manager, so every surface must share it."""
    from gobby.runner_init import servers, terminal_wiring
    from gobby.storage.workspaces import WorkspaceManager
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.leases import TerminalLeaseRegistry

    assert "workspace_manager" in GobbyRunner.__annotations__
    assert "workspace_manager" in {item.name for item in fields(ServiceContainer)}
    wiring_source = Path(terminal_wiring.__file__).read_text(encoding="utf-8")
    servers_source = " ".join(Path(servers.__file__).read_text(encoding="utf-8").split())
    assert wiring_source.count("WorkspaceManager(") == 1
    assert "runner.workspace_manager = WorkspaceManager(runner.database)" in wiring_source
    assert servers_source.count("WorkspaceManager(") == 0
    assert 'workspace_manager=getattr(runner, "workspace_manager", None),' in servers_source
    assert "workspace_manager=services.workspace_manager," in servers_source

    workspace_manager = WorkspaceManager(MagicMock(spec=HubDatabase))
    services = ServiceContainer(
        database=MagicMock(spec=HubDatabase),
        session_manager=MagicMock(spec=SessionManager),
        task_manager=MagicMock(spec=LocalTaskManager),
        workspace_manager=workspace_manager,
    )
    ws_config = MagicMock(spec=WebSocketConfig)
    ws_config.host = "localhost"
    ws_config.port = 60888
    ws_config.ping_interval = 30
    ws_config.ping_timeout = 10
    ws_config.max_message_size = 1024
    server = WebSocketServer(ws_config, MagicMock(), AsyncMock(return_value="test-user"))
    assert server.workspace_manager is None
    server.configure_terminals(
        MagicMock(),
        TerminalRuntimeRegistry(),
        lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
        write_coordinator=MagicMock(),
        workspace_manager=services.workspace_manager,
    )
    assert server.workspace_manager is workspace_manager
    # Without a session manager no actor can be scoped, so the server serves no ops.
    assert server.workspace_ops is None


def test_configure_terminals_installs_input_activity_sink() -> None:
    from gobby.terminals.host_events import InputActivityEvent

    class RecordingHost:
        def __init__(self) -> None:
            self.sinks: list[object] = []

        def set_input_activity_sink(self, sink: object) -> None:
            self.sinks.append(sink)

    class RecordingObserver:
        def __init__(self) -> None:
            self.inputs: list[tuple[str, str, str]] = []

        def record_mediated_input(self, terminal_id: str, payload: str, outcome: str) -> None:
            self.inputs.append((terminal_id, payload, outcome))

    class RecordingCoordinator:
        def __init__(self) -> None:
            self.observed: list[str] = []

        def observe_operator_input(self, terminal_id: str) -> None:
            self.observed.append(terminal_id)

    ws_config = MagicMock(spec=WebSocketConfig)
    ws_config.host = "localhost"
    ws_config.port = 60888
    ws_config.ping_interval = 30
    ws_config.ping_timeout = 10
    ws_config.max_message_size = 1024
    server = WebSocketServer(ws_config, MagicMock(), AsyncMock(return_value="test-user"))
    host = RecordingHost()
    coordinator = RecordingCoordinator()
    observer = RecordingObserver()
    server.configure_terminals(
        MagicMock(),
        MagicMock(),
        None,
        host_manager=host,
        lease_registry=MagicMock(),
        write_coordinator=coordinator,
    )
    assert host.sinks == [server._observe_input_activity]
    server.terminal_turn_observer = cast(Any, observer)

    sink = server._observe_input_activity
    sink(InputActivityEvent("t-1", "ht-1", "att-1", "input", 1, "ctrl_c", "epoch-1", 9))
    sink(InputActivityEvent("t-1", "ht-1", "att-1", "input", 1, "esc", "epoch-1", 10))
    sink(InputActivityEvent("t-2", "ht-2", "att-2", "paste", 3, None, "epoch-1", 11))
    assert observer.inputs == [
        ("t-1", "\x03", "delivered"),
        ("t-1", "\x1b", "delivered"),
        ("t-2", "", "delivered"),
    ]
    assert coordinator.observed == ["t-1", "t-1", "t-2"]

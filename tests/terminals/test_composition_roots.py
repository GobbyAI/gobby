"""Composition-root identity for terminal services (plan 2.2.7)."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
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


def test_single_instance_reaches_every_consumer() -> None:
    from gobby.config.terminals import TerminalConfig
    from gobby.storage.terminals import TerminalManager
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.services import TerminalServices

    annotations = GobbyRunner.__annotations__
    assert "terminal_manager" in annotations
    assert "terminal_runtime_registry" in annotations
    assert "terminal_config" in annotations
    assert "frame_client" in annotations
    assert "terminal_services" in annotations

    container_names = {item.name for item in fields(ServiceContainer)}
    assert "terminal_manager" in container_names
    assert "terminal_runtime_registry" in container_names
    assert "terminal_config" in container_names
    assert "frame_client" in container_names
    assert "terminal_services" in container_names

    manager = MagicMock(spec=TerminalManager)
    registry = TerminalRuntimeRegistry()
    registry.register(FakeRuntime(backend="tmux"))
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
        terminal_services=TerminalServices(manager=manager, registry=registry),
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
        manager, registry, config, terminal_services=services.terminal_services
    )
    assert server.terminal_manager is manager
    assert server.terminal_services is services.terminal_services
    assert server.terminal_runtime_registry is registry
    assert server.terminal_config is config
    assert server.terminal_manager is services.terminal_manager
    assert server.terminal_runtime_registry is services.terminal_runtime_registry
    assert server.terminal_config is services.terminal_config


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
    from gobby.runner_init import orchestration

    source = Path(orchestration.__file__).read_text(encoding="utf-8")
    assert source.count("TerminalServices(") == 1
    assert "runner.terminal_services = TerminalServices(" in source
    assert "terminal_services=runner.terminal_services," in source


def test_composition_roots_give_the_coordinator_the_registry() -> None:
    """Neither root may bind one runtime; the coordinator resolves per terminal."""
    from gobby.agents import lifecycle_monitor
    from gobby.runner_init import orchestration

    orchestration_source = Path(orchestration.__file__).read_text(encoding="utf-8")
    flattened = " ".join(orchestration_source.split())
    assert "WriteCoordinator(runner.terminal_manager, terminal_runtime_registry)" in flattened
    # The single-runtime resolve is what broke every write to a native terminal.
    assert 'resolve("tmux")' not in orchestration_source

    monitor_source = Path(lifecycle_monitor.__file__).read_text(encoding="utf-8")
    assert "WriteCoordinator(manager, registry)" in " ".join(monitor_source.split())

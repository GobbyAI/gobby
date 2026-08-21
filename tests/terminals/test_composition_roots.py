"""Composition-root identity for terminal services (plan 2.2.7)."""

from __future__ import annotations

from dataclasses import fields
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

    annotations = GobbyRunner.__annotations__
    assert "terminal_manager" in annotations
    assert "terminal_runtime_registry" in annotations
    assert "terminal_config" in annotations
    assert "frame_client" in annotations

    container_names = {item.name for item in fields(ServiceContainer)}
    assert "terminal_manager" in container_names
    assert "terminal_runtime_registry" in container_names
    assert "terminal_config" in container_names
    assert "frame_client" in container_names

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
    server.configure_terminals(manager, registry, config)
    assert server.terminal_manager is manager
    assert server.terminal_runtime_registry is registry
    assert server.terminal_config is config
    assert server.terminal_manager is services.terminal_manager
    assert server.terminal_runtime_registry is services.terminal_runtime_registry
    assert server.terminal_config is services.terminal_config

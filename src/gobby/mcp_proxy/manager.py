"""
Compatibility facade for multiple MCP client connections.

Implementation lives in :mod:`gobby.mcp_proxy.client_manager` so this module can
remain the stable public import and test patch point.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from mcp import ClientSession

from gobby.mcp_proxy.client_manager import (
    connections,
    health,
    invocation,
    secrets,
    server_registry,
    tool_inventory,
)
from gobby.mcp_proxy.lazy import LazyServerConnector, RetryConfig
from gobby.mcp_proxy.models import (
    ConnectionState,
    HealthState,
    MCPConnectionHealth,
    MCPError,
    MCPServerConfig,
)
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from gobby.mcp_proxy.transports.factory import create_transport_connection

_create_transport_connection = create_transport_connection

__all__ = [
    "MCPClientManager",
    "MCPServerConfig",
    "ConnectionState",
    "HealthState",
    "MCPConnectionHealth",
    "MCPError",
    "truncate_tool_brief",
]

logger = logging.getLogger("gobby.mcp.manager")


def truncate_tool_brief(text: str | None, *, max_chars: int = 100) -> str:
    return server_registry.truncate_tool_brief(text, max_chars=max_chars)


_truncate_tool_brief = truncate_tool_brief


class MCPClientManager:
    """Manages multiple MCP client connections with shared authentication."""

    def __init__(
        self,
        server_configs: list[MCPServerConfig] | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
        health_check_interval: float = 60.0,
        external_id: str | None = None,
        project_path: str | None = None,
        project_id: str | None = None,
        mcp_db_manager: Any | None = None,
        lazy_connect: bool = True,
        preconnect_servers: list[str] | None = None,
        connection_timeout: float = 30.0,
        max_connection_retries: int = 3,
        metrics_manager: Any | None = None,
    ):
        self._connections: dict[str, BaseTransportConnection] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self.health: dict[str, MCPConnectionHealth] = {}
        self._token_refresh_callback = token_refresh_callback
        self._health_check_interval = health_check_interval
        self._health_check_task: asyncio.Task[None] | None = None
        self._reconnect_tasks: set[asyncio.Task[None]] = set()
        self._auth_token: str | None = None
        self._running = False
        self.external_id = external_id
        self.project_path = project_path
        self.project_id = project_id
        self.mcp_db_manager = mcp_db_manager
        self.metrics_manager = metrics_manager
        self.lazy_connect = lazy_connect
        self.preconnect_servers = set(preconnect_servers or [])
        self.connection_timeout = connection_timeout
        self.max_connection_retries = max_connection_retries
        self._lazy_connector = LazyServerConnector(
            retry_config=RetryConfig(max_retries=max_connection_retries),
        )
        server_registry.load_initial_configs(self, server_configs, logger)

    @staticmethod
    def load_tools_from_db(
        mcp_db_manager: Any,
        server_name: str,
        project_id: str,
    ) -> list[dict[str, str]] | None:
        return server_registry.load_tools_from_db(
            mcp_db_manager,
            server_name,
            project_id,
            logger,
        )

    @staticmethod
    def _load_tools_from_db(
        mcp_db_manager: Any,
        server_name: str,
        project_id: str,
    ) -> list[dict[str, str]] | None:
        return MCPClientManager.load_tools_from_db(mcp_db_manager, server_name, project_id)

    @property
    def connections(self) -> dict[str, BaseTransportConnection]:
        return self._connections

    @property
    def server_configs(self) -> list[MCPServerConfig]:
        return server_registry.server_configs(self)

    def list_connections(self) -> list[MCPServerConfig]:
        return server_registry.list_connections(self)

    def get_available_servers(self) -> list[str]:
        return server_registry.get_available_servers(self)

    def get_client(self, server_name: str) -> BaseTransportConnection:
        return server_registry.get_client(self, server_name)

    def has_server(self, server_name: str) -> bool:
        return server_registry.has_server(self, server_name)

    def get_server_config(self, name: str) -> MCPServerConfig | None:
        return server_registry.get_server_config(self, name)

    def is_connected(self, name: str) -> bool:
        return server_registry.is_connected(self, name)

    async def add_server(self, config: MCPServerConfig) -> dict[str, Any]:
        return await server_registry.add_server(self, config)

    async def remove_server(self, name: str, project_id: str | None = None) -> dict[str, Any]:
        return await server_registry.remove_server(self, name, project_id)

    async def set_server_enabled(
        self, name: str, enabled: bool, project_id: str | None = None
    ) -> dict[str, Any]:
        return await server_registry.set_server_enabled(self, name, enabled, project_id)

    async def disconnect_server(self, name: str) -> None:
        await connections.disconnect_server(self, name, logger)

    async def get_health_report(self) -> dict[str, Any]:
        return self.get_server_health()

    async def connect_all(self, configs: list[MCPServerConfig] | None = None) -> dict[str, bool]:
        return await connections.connect_all(self, configs)

    def get_lazy_connection_states(self) -> dict[str, dict[str, Any]]:
        return self._lazy_connector.get_all_states()

    async def health_check_all(self) -> dict[str, Any]:
        return await health.health_check_all(self)

    def _resolve_secrets_in_config(self, config: MCPServerConfig) -> MCPServerConfig:
        return secrets.resolve_secrets_in_config(self, config, logger)

    async def _connect_server(self, config: MCPServerConfig) -> ClientSession | None:
        return await connections.connect_server(
            self,
            config,
            create_transport_connection,
        )

    async def disconnect_all(self) -> None:
        await connections.disconnect_all(self, logger)

    async def ensure_connected(self, server_name: str) -> ClientSession:
        return await connections.ensure_connected(self, server_name)

    async def get_client_session(self, server_name: str) -> ClientSession:
        return await connections.get_client_session(self, server_name)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
        session_id: str | None = None,
    ) -> Any:
        return await invocation.call_tool(
            self,
            server_name,
            tool_name,
            arguments,
            timeout,
            session_id,
            logger,
        )

    async def read_resource(self, server_name: str, uri: str) -> Any:
        return await invocation.read_resource(self, server_name, uri)

    async def list_tools(self, server_name: str | None = None) -> dict[str, list[dict[str, Any]]]:
        return await tool_inventory.list_tools(self, server_name, logger)

    async def _list_tools_for_server(self, server_name: str) -> list[dict[str, Any]]:
        return await tool_inventory.list_tools_for_server(self, server_name, logger)

    async def _retry_list_tools_after_failure(
        self,
        server_name: str,
        initial_error: Exception,
    ) -> list[dict[str, Any]]:
        return await tool_inventory.retry_list_tools_after_failure(
            self,
            server_name,
            initial_error,
            logger,
        )

    @staticmethod
    async def _list_tools_from_session(session: ClientSession) -> list[dict[str, Any]]:
        return await tool_inventory.list_tools_from_session(session)

    def cache_discovered_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None:
        tool_inventory.cache_discovered_tools(self, server_name, tools)

    def _cache_discovered_tools(self, server_name: str, tools: list[dict[str, Any]]) -> None:
        self.cache_discovered_tools(server_name, tools)

    async def get_tool_input_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        return await tool_inventory.get_tool_input_schema(self, server_name, tool_name)

    async def get_tool_info(self, server_name: str, tool_name: str) -> dict[str, Any]:
        return await tool_inventory.get_tool_info(self, server_name, tool_name)

    async def _monitor_health(self) -> None:
        await health.monitor_health(self, logger, asyncio.sleep)

    async def _reconnect(self, server_name: str) -> None:
        await connections.reconnect(self, server_name, logger)

    def get_server_health(self) -> dict[str, dict[str, Any]]:
        return health.get_server_health(self)

    def add_server_config(self, config: MCPServerConfig) -> None:
        server_registry.add_server_config(self, config)

    def remove_server_config(self, name: str) -> None:
        server_registry.remove_server_config(self, name, logger)

"""
Compatibility facade for multiple MCP client connections.

Implementation lives in :mod:`gobby.mcp_proxy.client_manager` so this module can
remain the stable public import and test patch point.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
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
]

logger = logging.getLogger("gobby.mcp.manager")


class MCPClientManager:
    """Manage multiple MCP client connections."""

    def __init__(
        self,
        server_configs: list[MCPServerConfig] | None = None,
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
        stdio_errlog_path: str | None = None,
        template_expand: Callable[..., Mapping[str, Any]] | None = None,
    ):
        self._connections: dict[str, BaseTransportConnection] = {}
        self._configs: dict[str, MCPServerConfig] = {}
        self._tool_schema_cache: dict[str, list[dict[str, Any]]] = {}
        self._tool_cache_dirty: set[str] = set()
        self.health: dict[str, MCPConnectionHealth] = {}
        self._health_check_interval = health_check_interval
        self._health_check_task: asyncio.Task[None] | None = None
        self._reconnect_tasks: set[asyncio.Task[None]] = set()
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
        self.stdio_errlog_path = stdio_errlog_path
        self._lazy_connector = LazyServerConnector(
            retry_config=RetryConfig(max_retries=max_connection_retries),
        )
        self._template_expand = (
            template_expand if template_expand is not None else self._default_template_expand
        )
        server_registry.load_initial_configs(self, server_configs, logger)

    def _default_template_expand(self, template_row: Any, server: Any) -> Mapping[str, Any]:
        from gobby.mcp_proxy.templates import expand_server_instance
        from gobby.storage.secrets import SecretStore

        db = getattr(self.mcp_db_manager, "db", None)
        store = SecretStore(db) if db is not None else None

        def secret_exists(name: str) -> bool:
            if store is None:
                return False
            return bool(store.exists(name, project_id=str(server.project_id)))

        definition = getattr(template_row, "definition", {}) or {}
        return expand_server_instance(
            definition,
            name=server.name,
            project_id=str(server.project_id),
            template_values=getattr(server, "template_values", None),
            description=getattr(server, "description", None),
            secret_exists=secret_exists,
        )

    @staticmethod
    def load_tools_from_db(
        mcp_db_manager: Any,
        server_id: str,
    ) -> list[dict[str, str]] | None:
        return server_registry.load_tools_from_db(
            mcp_db_manager,
            server_id,
            logger,
        )

    @staticmethod
    def _load_tools_from_db(
        mcp_db_manager: Any,
        server_id: str,
    ) -> list[dict[str, str]] | None:
        return MCPClientManager.load_tools_from_db(mcp_db_manager, server_id)

    @property
    def connections(self) -> dict[str, BaseTransportConnection]:
        return self._connections

    @property
    def server_configs(self) -> list[MCPServerConfig]:
        return server_registry.server_configs(self)

    def list_connections(self) -> list[MCPServerConfig]:
        return server_registry.list_connections(self)

    def get_available_servers(self, *, project_id: str) -> list[str]:
        return server_registry.get_available_servers(self, project_id=project_id)

    def get_client(self, server_id: str) -> BaseTransportConnection:
        return server_registry.get_client(self, server_id)

    def has_server(self, server_id: str) -> bool:
        return server_registry.has_server(self, server_id)

    def get_server_config(self, server_id: str) -> MCPServerConfig | None:
        return server_registry.get_server_config(self, server_id)

    def is_connected(self, server_id: str) -> bool:
        return server_registry.is_connected(self, server_id)

    async def add_server(self, config: MCPServerConfig) -> dict[str, Any]:
        return await server_registry.add_server(self, config)

    async def remove_server(self, server_id: str, project_id: str | None = None) -> dict[str, Any]:
        return await server_registry.remove_server(self, server_id, project_id)

    async def update_server(
        self,
        server_id: str,
        config: MCPServerConfig | Mapping[str, Any],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return await server_registry.update_server(self, server_id, config, project_id)

    async def set_server_description(self, server_id: str, description: str) -> None:
        await server_registry.set_server_description(self, server_id, description)

    async def set_server_enabled(
        self, server_id: str, enabled: bool, project_id: str | None = None
    ) -> dict[str, Any]:
        return await server_registry.set_server_enabled(self, server_id, enabled, project_id)

    async def disconnect_server(self, server_id: str) -> None:
        await connections.disconnect_server(self, server_id, logger)

    async def refresh_server(self, server_id: str) -> None:
        await server_registry.refresh_server(self, server_id)

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
        def create_connection(resolved_config: MCPServerConfig) -> BaseTransportConnection:
            return create_transport_connection(
                resolved_config,
                stdio_errlog_path=self.stdio_errlog_path,
            )

        return await connections.connect_server(
            self,
            config,
            create_connection,
        )

    async def disconnect_all(self) -> None:
        await connections.disconnect_all(self, logger)

    async def ensure_connected(self, server_id: str) -> ClientSession:
        return await connections.ensure_connected(self, server_id)

    async def get_client_session(self, server_id: str) -> ClientSession:
        return await connections.get_client_session(self, server_id)

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
        session_id: str | None = None,
    ) -> Any:
        return await invocation.call_tool(
            self,
            server_id,
            tool_name,
            arguments,
            timeout,
            session_id,
            logger,
        )

    async def read_resource(self, server_id: str, uri: str) -> Any:
        return await invocation.read_resource(self, server_id, uri)

    async def list_tools(
        self,
        server_id: str | None = None,
        *,
        project_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        return await tool_inventory.list_tools(self, server_id, logger, project_id=project_id)

    async def _list_tools_for_server(self, server_id: str) -> list[dict[str, Any]]:
        return await tool_inventory.list_tools_for_server(self, server_id, logger)

    async def _retry_list_tools_after_failure(
        self,
        server_id: str,
        initial_error: Exception,
    ) -> list[dict[str, Any]]:
        return await tool_inventory.retry_list_tools_after_failure(
            self,
            server_id,
            initial_error,
            logger,
        )

    @staticmethod
    async def _list_tools_from_session(session: ClientSession) -> list[dict[str, Any]]:
        return await tool_inventory.list_tools_from_session(session)

    def cache_discovered_tools(self, server_id: str, tools: list[dict[str, Any]]) -> None:
        tool_inventory.cache_discovered_tools(self, server_id, tools)

    def _cache_discovered_tools(self, server_id: str, tools: list[dict[str, Any]]) -> None:
        self.cache_discovered_tools(server_id, tools)

    async def get_tool_input_schema(self, server_id: str, tool_name: str) -> dict[str, Any]:
        return await tool_inventory.get_tool_input_schema(self, server_id, tool_name)

    async def get_tool_info(self, server_id: str, tool_name: str) -> dict[str, Any]:
        return await tool_inventory.get_tool_info(self, server_id, tool_name)

    async def _monitor_health(self) -> None:
        await health.monitor_health(self, logger, asyncio.sleep)

    async def _reconnect(self, server_id: str) -> None:
        await connections.reconnect(self, server_id, logger)

    def get_server_health(self) -> dict[str, dict[str, Any]]:
        return health.get_server_health(self)

    def add_server_config(self, config: MCPServerConfig) -> None:
        server_registry.add_server_config(self, config)

    def remove_server_config(self, server_id: str) -> None:
        server_registry.remove_server_config(self, server_id, logger)

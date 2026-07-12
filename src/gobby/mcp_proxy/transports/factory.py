"""Transport connection factory."""

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from gobby.mcp_proxy.transports.http import HTTPTransportConnection
from gobby.mcp_proxy.transports.sse import SSETransportConnection
from gobby.mcp_proxy.transports.stdio import StdioTransportConnection
from gobby.mcp_proxy.transports.websocket import WebSocketTransportConnection


def create_transport_connection(
    config: MCPServerConfig,
    stdio_errlog_path: str | None = None,
) -> BaseTransportConnection:
    """
    Factory function to create appropriate transport connection.

    Args:
        config: Server configuration
        stdio_errlog_path: Optional stderr log path for stdio child processes

    Returns:
        Transport-specific connection instance

    Raises:
        ValueError: If transport type is unsupported
    """
    transport_map: dict[str, type[BaseTransportConnection]] = {
        "http": HTTPTransportConnection,
        "sse": SSETransportConnection,
        "stdio": StdioTransportConnection,
        "websocket": WebSocketTransportConnection,
    }

    transport_class = transport_map.get(config.transport)
    if not transport_class:
        raise ValueError(
            f"Unsupported transport: {config.transport}. Supported: {list(transport_map.keys())}"
        )

    if transport_class is StdioTransportConnection:
        return StdioTransportConnection(
            config,
            stdio_errlog_path=stdio_errlog_path,
        )

    return transport_class(config)

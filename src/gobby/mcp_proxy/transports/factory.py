"""Transport connection factory."""

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.transport_types import SUPPORTED_TRANSPORTS
from gobby.mcp_proxy.transports.base import BaseTransportConnection
from gobby.mcp_proxy.transports.http import HTTPTransportConnection
from gobby.mcp_proxy.transports.sse import SSETransportConnection
from gobby.mcp_proxy.transports.stdio import StdioTransportConnection
from gobby.mcp_proxy.transports.websocket import WebSocketTransportConnection

TRANSPORT_CONNECTION_TYPES: dict[str, type[BaseTransportConnection]] = dict(
    zip(
        SUPPORTED_TRANSPORTS,
        (
            HTTPTransportConnection,
            SSETransportConnection,
            StdioTransportConnection,
            WebSocketTransportConnection,
        ),
        strict=True,
    )
)


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
    transport_class = TRANSPORT_CONNECTION_TYPES.get(config.transport)
    if not transport_class:
        raise ValueError(
            f"Unsupported transport: {config.transport}. Supported: {list(SUPPORTED_TRANSPORTS)}"
        )

    if transport_class is StdioTransportConnection:
        return StdioTransportConnection(
            config,
            stdio_errlog_path=stdio_errlog_path,
        )

    return transport_class(config)

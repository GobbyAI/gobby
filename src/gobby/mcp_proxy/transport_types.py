"""Shared MCP transport type definitions."""

SUPPORTED_TRANSPORTS = ("http", "sse", "stdio", "websocket")
URL_TRANSPORTS = frozenset(transport for transport in SUPPORTED_TRANSPORTS if transport != "stdio")

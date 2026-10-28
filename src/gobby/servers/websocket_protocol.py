"""WebSocket protocol used by the daemon's ASGI listener."""

from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol


class WebSocketProtocol(WebSocketsSansIOProtocol):
    """Daemon-owned transport boundary for Uvicorn's SansIO implementation."""

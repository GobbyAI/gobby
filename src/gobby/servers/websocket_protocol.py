"""WebSocket protocol used by the daemon's ASGI listener."""

from uvicorn._types import ASGISendEvent
from uvicorn.protocols.utils import ClientDisconnected
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol


class WebSocketProtocol(WebSocketsSansIOProtocol):
    """Daemon-owned transport boundary for Uvicorn's SansIO implementation."""

    _disconnected = False

    def connection_lost(self, exc: Exception | None) -> None:
        self._disconnected = True
        self.writable.set()
        super().connection_lost(exc)

    async def send(self, message: ASGISendEvent) -> None:
        # SansIO queues a disconnect on TCP loss but leaves its frame state open.
        # Writers need an error even when no receiver consumes that notification.
        if self._disconnected or self.transport.is_closing():
            raise ClientDisconnected()
        await self.writable.wait()
        if self._disconnected or self.transport.is_closing():
            raise ClientDisconnected()
        await super().send(message)

"""Transport helpers for chat response streaming."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class ChatStreamOwner(Protocol):
    clients: dict[ServerConnection, dict[str, Any]]


@dataclass
class ChatStreamTransport:
    """Request-scoped WebSocket message builder and sender."""

    owner: ChatStreamOwner
    websocket: ServerConnection
    conversation_id: str
    request_id: str
    ws_connected: bool = True

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        """Build a response dict with the request_id correlation field."""
        msg: dict[str, Any] = fields
        msg["request_id"] = self.request_id
        return msg

    async def send_direct(self, msg: dict[str, Any]) -> None:
        """Send directly to the request websocket."""
        await self.websocket.send(json.dumps(msg))

    async def safe_send(self, msg: dict[str, Any]) -> bool:
        """Broadcast to all WebSocket clients bound to this conversation."""
        if not self.ws_connected:
            return False

        encoded = json.dumps(msg)
        any_sent = False
        for ws, meta in list(self.owner.clients.items()):
            cid = meta.get("conversation_id") if meta else None
            if cid != self.conversation_id:
                continue
            try:
                await ws.send(encoded)
                any_sent = True
            except ConnectionClosed:
                pass

        if not any_sent:
            self.ws_connected = False
            logger.debug(
                "All clients disconnected during chat stream for %s",
                self.conversation_id[:8],
            )
        return any_sent

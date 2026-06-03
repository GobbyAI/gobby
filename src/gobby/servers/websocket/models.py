"""WebSocket models and configuration.

Protocol definitions, configuration dataclass, and constants for the
WebSocket server. Extracted from websocket.py as part of the Strangler
Fig decomposition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT

# Idle session cleanup interval and timeout
CLEANUP_INTERVAL_SECONDS = 300  # 5 minutes
IDLE_TIMEOUT_SECONDS = 1800  # 30 minutes


class WebSocketClient(Protocol):
    """Protocol for WebSocket connection to include custom attributes."""

    user_id: str
    subscriptions: set[str]
    latency: float
    remote_address: Any

    async def send(self, message: str) -> None: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...
    async def wait_closed(self) -> None: ...
    def __aiter__(self) -> Any: ...


@dataclass
class WebSocketConfig:
    """Configuration for WebSocket server."""

    host: str = "localhost"
    port: int = DEFAULT_WEBSOCKET_PORT
    ping_interval: int = 30  # seconds
    ping_timeout: int = 10  # seconds
    max_message_size: int = 5 * 1024 * 1024  # 5MB (increased for voice audio)

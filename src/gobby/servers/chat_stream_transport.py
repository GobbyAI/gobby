"""Transport contract shared by streamed ChatSession surfaces."""

from __future__ import annotations

from typing import Any, Protocol


class ChatStreamTransport(Protocol):
    """Request-scoped destination for normalized ChatSession stream frames."""

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        """Build a transport-specific response frame."""
        ...

    async def send_direct(self, msg: dict[str, Any]) -> None:
        """Send a frame to the initiating client or conversation."""
        ...

    async def safe_send(self, msg: dict[str, Any]) -> bool:
        """Send a stream frame and report whether processing should continue."""
        ...

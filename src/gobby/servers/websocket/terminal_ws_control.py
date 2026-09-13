"""Writer-control handlers for terminal WebSocket attachments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.terminals.leases import TerminalLeaseRegistry


class TerminalControlMixin:
    """Take and release the registry-owned writer lease."""

    if TYPE_CHECKING:

        def _leases(self) -> TerminalLeaseRegistry: ...

        async def _fanout_lease_lost(
            self,
            previous: str,
            holder: str,
            generation: int,
            *,
            requester: Any | None = None,
        ) -> None: ...

        async def _send_control(self, websocket: Any, payload: dict[str, Any]) -> None: ...

        async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None: ...

    async def _handle_terminal_take_control(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = str(data.get("terminal_id") or "")
        attachment_id = str(data.get("attachment_id") or "")
        result = await self._leases().take_control(
            terminal_id,
            attachment_id,
            takeover=bool(data.get("takeover")),
        )
        displaced = result.displaced_attachment_id
        if result.granted and displaced is not None:
            await self._fanout_lease_lost(
                displaced,
                attachment_id,
                result.lease_generation,
                requester=websocket,
            )
        await self._send_control(
            websocket,
            {
                "type": "terminal_control_result",
                "attachment_id": attachment_id,
                "granted": result.granted,
                "reason": result.reason,
                "lease_generation": result.lease_generation,
            },
        )

    async def _handle_terminal_release_control(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = str(data.get("attachment_id") or "")
        result = await self._leases().release_control(attachment_id)
        await self._send_json(
            websocket,
            {
                "type": "terminal_control_result",
                "attachment_id": attachment_id,
                "granted": result.granted,
                "reason": result.reason,
                "lease_generation": result.lease_generation,
            },
        )

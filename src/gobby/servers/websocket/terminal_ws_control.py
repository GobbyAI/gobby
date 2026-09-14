"""Writer-control handlers for terminal WebSocket attachments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.terminals.leases import SizingDecision, TerminalLeaseRegistry


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

        async def _apply_terminal_sizing(
            self, terminal_id: str, sizing: SizingDecision | None
        ) -> None: ...

    async def _handle_terminal_take_control(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = str(data.get("terminal_id") or "")
        attachment_id = str(data.get("attachment_id") or "")
        result = await self._leases().take_control(
            terminal_id,
            attachment_id,
            takeover=bool(data.get("takeover")),
        )
        # Holding the lease is what lets a web viewer size the shared terminal. Apply
        # the decision before any socket send can stall, so a later take's size wins.
        await self._apply_terminal_sizing(terminal_id, result.sizing)
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
        record = self._leases().get(attachment_id)
        result = await self._leases().release_control(attachment_id)
        if record is not None:
            await self._apply_terminal_sizing(record.terminal_id, result.sizing)
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

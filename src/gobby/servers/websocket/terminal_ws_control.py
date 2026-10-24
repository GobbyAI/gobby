"""Writer-control handlers for terminal WebSocket attachments."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.terminals.leases import SizingDecision, TerminalLeaseRegistry

logger = logging.getLogger(__name__)


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

        async def _declare_holder_theme(self, attachment_id: str) -> None: ...

    async def _refuse_unowned_control(self, websocket: Any, attachment_id: str) -> None:
        """Answer a control request for an attachment this connection did not create.

        The answer matches an unknown attachment, so a foreign connection learns nothing.
        """
        await self._send_json(
            websocket,
            {
                "type": "terminal_control_result",
                "attachment_id": attachment_id,
                "granted": False,
                "reason": "stale_attachment",
                "lease_generation": None,
                "host_input_granted": None,
            },
        )

    async def _handle_terminal_take_control(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = str(data.get("terminal_id") or "")
        attachment_id = str(data.get("attachment_id") or "")
        if self._leases().owned_attachment(attachment_id, websocket=websocket) is None:
            await self._refuse_unowned_control(websocket, attachment_id)
            return
        host_attachment_id = data.get("host_attachment_id")
        if host_attachment_id is not None and (
            type(host_attachment_id) is not int or not 0 < host_attachment_id < 2**64
        ):
            await self._send_control(
                websocket,
                {
                    "type": "terminal_control_result",
                    "attachment_id": attachment_id,
                    "granted": False,
                    "reason": "invalid_host_attachment_id",
                    "lease_generation": self._leases().generation(terminal_id),
                    "host_input_granted": False,
                },
            )
            return
        result = await self._leases().take_control(
            terminal_id,
            attachment_id,
            takeover=bool(data.get("takeover")),
            host_attachment_id=host_attachment_id,
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
                "host_input_granted": result.host_input_granted,
            },
        )
        if result.granted:
            await self._declare_holder_theme(attachment_id)

    async def _handle_terminal_focus(self, websocket: Any, data: dict[str, Any]) -> None:
        # The viewer's focus report is fire-and-forget: no reply, and a stale
        # attachment only logs, because the lease already moved on.
        attachment_id = str(data.get("attachment_id") or "")
        focused = bool(data.get("focused"))
        if self._leases().owned_attachment(attachment_id, websocket=websocket) is None:
            logger.debug("terminal_focus ignored for unowned attachment %s", attachment_id)
            return
        if not await self._leases().set_focus(attachment_id, focused):
            logger.debug("terminal_focus ignored for stale attachment %s", attachment_id)

    async def _handle_terminal_release_control(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = str(data.get("attachment_id") or "")
        if self._leases().owned_attachment(attachment_id, websocket=websocket) is None:
            await self._refuse_unowned_control(websocket, attachment_id)
            return
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
                "host_input_granted": result.host_input_granted,
            },
        )

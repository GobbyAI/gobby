"""Terminal sizing handlers split from the WebSocket transport mixin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.terminals.dimensions import InvalidTerminalDimensionsError
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.leases import SizingDecision, TerminalLeaseRegistry

logger = logging.getLogger(__name__)


class TerminalSizingMixin:
    terminal_manager: Any

    if TYPE_CHECKING:

        async def _send_json(self, websocket: Any, payload: dict[str, Any]) -> None: ...

        def _leases(self) -> TerminalLeaseRegistry: ...

        def _proxy(self) -> Any: ...

        def _runtime_for(self, backend: str) -> Any | None: ...

    async def _handle_terminal_resize(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        if not isinstance(attachment_id, str):
            return
        admitted = self._leases().resize_pty(attachment_id, data.get("rows"), data.get("cols"))
        if not admitted.ok:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_error",
                    "code": admitted.reason,
                    "attachment_id": attachment_id,
                },
            )
            return
        record = self._leases().get(attachment_id)
        if record is None:
            return
        # The sender's own grid changed whether or not it owns the PTY size;
        # host frames for this attachment must render at the new grid, so the
        # viewport follows the resize instead of staying at the rendezvous size.
        if record.geometry is not None:
            await self._sync_attachment_viewport(attachment_id, *record.geometry)
        if not admitted.applied:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_resize_result",
                    "attachment_id": attachment_id,
                    "applied": False,
                    "owner_viewer": admitted.owner_viewer,
                },
            )
            return
        await self._apply_terminal_sizing(record.terminal_id, admitted.sizing)

    async def _apply_terminal_sizing(self, terminal_id: str, sizing: SizingDecision | None) -> None:
        if sizing is None:
            return
        manager = self.terminal_manager
        if manager is None:
            return
        row = manager.get(terminal_id)
        if row is None:
            return
        runtime = self._runtime_for(row.backend)
        if runtime is None:
            return
        if sizing.owner_viewer is None:
            if row.backend == "tmux":
                release_size = getattr(runtime, "release_size", None)
                if callable(release_size):
                    await release_size(row)
            return
        rows, cols = sizing.rows, sizing.cols
        if rows is None or cols is None:
            return
        # A released tmux window follows its clients again, so the recorded dims
        # no longer prove the pin is in place: re-pin even at the same geometry.
        if row.backend != "tmux" and (row.rows, row.cols) == (rows, cols):
            return
        try:
            await runtime.resize(row, rows, cols)
        except HostCommandError as exc:
            # An exited pane or a draining host refuses typed. Killing the
            # WebSocket handler here drops the client's resize reply and leaves
            # its grid drawn at a size the pane never took; the recorded dims
            # stay stale instead, so the next resize retries rather than
            # short-circuiting on matching geometry.
            logger.warning(
                "Terminal %s resize to %sx%s refused by the host: %s",
                terminal_id,
                rows,
                cols,
                exc.error,
            )
            return
        manager.set_dims(row.id, rows, cols)

    async def _handle_terminal_set_viewport(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        if not isinstance(attachment_id, str):
            return
        try:
            await self._sync_attachment_viewport(attachment_id, data.get("rows"), data.get("cols"))
        except (KeyError, InvalidTerminalDimensionsError):
            await self._send_json(
                websocket, {"type": "terminal_error", "code": "invalid_dimensions"}
            )

    async def _sync_attachment_viewport(
        self, attachment_id: str, rows: object, cols: object
    ) -> None:
        """Record the attachment's viewport and push it to its proxy frame."""
        rows, cols = self._leases().set_viewport(attachment_id, rows, cols)
        frame = self._proxy().frame_for(attachment_id)
        setter = getattr(frame, "set_viewport", None)
        if callable(setter):
            await setter(rows, cols)

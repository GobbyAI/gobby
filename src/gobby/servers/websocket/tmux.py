"""WebSocket handlers for tmux-backed terminal sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.servers.websocket.terminal_sizing import TerminalSizingMixin
from gobby.servers.websocket.terminal_ws import TerminalWsMixin
from gobby.servers.websocket.terminal_ws_create import TerminalCreateMixin
from gobby.servers.websocket.tmux_activation import (
    STATE_ACTIVATING,
    STATE_RESERVED,
    PendingAttachment,
    activate_attachment,
    cancel_all_pending,
    cancel_pending_for_owner,
    cancel_stale_reservations,
    teardown_bridge,
)
from gobby.terminals.dimensions import validate_dimensions
from gobby.terminals.leases import TerminalLeaseRegistry

logger = logging.getLogger(__name__)

# Default server config (no socket = user's default tmux)
_DEFAULT_CONFIG = TmuxConfig(socket_name="")
_GOBBY_CONFIG = TmuxConfig(socket_name="gobby")


class TmuxMixin(TerminalCreateMixin, TerminalSizingMixin, TerminalWsMixin):
    """Mixin providing tmux session management handlers for WebSocketServer.

    Requires on the host class:
    - ``self.clients: dict[Any, dict[str, Any]]``
    - ``async self.broadcast_terminal_output(run_id, data)`` (from BroadcastMixin)
    - ``async self._send_error(websocket, message, ...)`` (from HandlerMixin)
    """

    clients: dict[Any, dict[str, Any]]

    # Set up by _init_tmux; declared here so the attachment state machine in
    # tmux_activation can type-check against this class structurally.
    _tmux_bridge: TmuxPTYBridge
    _tmux_mgr_gobby: TmuxSessionManager
    _tmux_mgr_default: TmuxSessionManager
    _tmux_client_bridges: dict[Any, set[str]]
    _tmux_pending: dict[str, PendingAttachment]

    # These are provided by other mixins (BroadcastMixin, HandlerMixin) or by
    # WebSocketServer itself (daemon_config, the live config-store projection).
    # Declared as TYPE_CHECKING-only protocol hints to avoid shadowing real methods.
    if TYPE_CHECKING:

        async def broadcast_terminal_output(
            self, terminal_id: str, data: str, attachment_id: str | None = None
        ) -> None: ...

        async def broadcast_tmux_session_event(
            self,
            event: str,
            terminal_id: str = "",
            session_name: str | None = None,
            socket: str | None = None,
            terminal: dict[str, Any] | None = None,
        ) -> None: ...

        async def _send_error(
            self, websocket: Any, message: str, request_id: str | None = None, code: str = "ERROR"
        ) -> None: ...

    async def _broadcast_tmux_event(
        self,
        event: str,
        session_name: str,
        socket: str,
    ) -> None:
        """Route the legacy tmux lifecycle seam through ordered terminal events."""
        await self.broadcast_tmux_session_event(
            event,
            terminal_id=session_name,
            session_name=session_name,
            socket=socket,
        )

    def _init_tmux(self) -> None:
        """Initialize tmux subsystem. Call from WebSocketServer.__init__."""
        self._tmux_bridge = TmuxPTYBridge()
        self._tmux_mgr_gobby = TmuxSessionManager(_GOBBY_CONFIG)
        self._tmux_mgr_default = TmuxSessionManager(_DEFAULT_CONFIG)
        # Track which client owns which bridge (for cleanup on disconnect)
        self._tmux_client_bridges: dict[Any, set[str]] = {}
        # Attachments acknowledged but not yet built (attachment_id -> reservation)
        self._tmux_pending: dict[str, PendingAttachment] = {}
        self.lease_registry = TerminalLeaseRegistry()

    async def _cleanup_tmux(self) -> None:
        """Detach every tmux client and proxy attachment. Call from WebSocketServer.stop."""
        cancel_all_pending(self)
        for attachment_id in list((await self._tmux_bridge.list_bridges()).keys()):
            await teardown_bridge(self, attachment_id)
        hub = getattr(self, "_proxy_hub", None)
        if hub is not None:
            for websocket in list(hub.relays):
                await hub.drop_socket(websocket, "ws_close")
        self._tmux_client_bridges.clear()

    async def _cleanup_tmux_client(self, websocket: Any) -> None:
        """Release bridges, leases, and host-frame attachments for a disconnecting client."""
        cancel_pending_for_owner(self, websocket)
        for attachment_id in list(self._tmux_client_bridges.get(websocket, ())):
            await teardown_bridge(self, attachment_id)
            logger.debug("Cleaned up tmux bridge %s for disconnected client", attachment_id)
        events = self._leases().finalize_websocket(websocket, "ws_close")
        for event in events:
            await self._apply_terminal_sizing(event.terminal_id, event.sizing)
        hub = getattr(self, "_proxy_hub", None)
        if hub is not None:
            await hub.drop_socket(websocket, "ws_close")
        self._tmux_client_bridges.pop(websocket, None)

    # ------------------------------------------------------------------
    # tmux rows attach through a real tmux client in a PTY sized to the
    # browser (the pre-herdr renderer); native rows keep the host proxy.
    # ------------------------------------------------------------------

    def _tmux_attach_target(self, row: Any) -> tuple[TmuxSessionManager, TmuxConfig, str] | None:
        """The session manager, config, and session name a tmux client needs for ``row``."""
        session_name = row.session_name or row.spawn_key
        if not isinstance(session_name, str) or not session_name:
            return None
        locator = row.locator if isinstance(row.locator, dict) else {}
        socket_path = locator.get("socket_path")
        if isinstance(socket_path, str) and socket_path:
            template = TmuxConfig(socket_name="", socket_path=socket_path)
        else:
            template = _DEFAULT_CONFIG if row.ownership == "external" else _GOBBY_CONFIG
        # The attach-history bound is the config store's to set, so the live
        # daemon value overlays the template's default.
        daemon = getattr(self, "daemon_config", None)
        config = (
            template
            if daemon is None
            else template.model_copy(
                update={"attach_history_lines": daemon.tmux.attach_history_lines}
            )
        )
        return TmuxSessionManager(config), config, session_name

    async def _tmux_bridge_for(self, attachment_id: object) -> Any | None:
        if not isinstance(attachment_id, str):
            return None
        return await self._tmux_bridge.get_bridge(attachment_id)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_terminal_attach(self, websocket: Any, data: dict[str, Any]) -> None:
        """Reserve a tmux-client attachment for a tmux row; other rows use the proxy.

        This deliberately builds nothing. ``TmuxPTYBridge.attach`` spawns
        ``tmux attach-session`` immediately, so creating the bridge here would
        start a tmux client painting at the hardcoded 50x200 default -- a
        full-screen paint plus a resize redraw, both at the wrong width, both
        delivered after the history as garbage. The client's first resize
        carries its real geometry; :func:`activate_attachment` builds there, so
        tmux attaches exactly once at the size the user is actually looking at.
        """
        terminal_id = data.get("terminal_id")
        manager = getattr(self, "terminal_manager", None)
        row = (
            manager.get(terminal_id)
            if manager is not None and isinstance(terminal_id, str) and terminal_id
            else None
        )
        target = None if row is None or row.backend != "tmux" else self._tmux_attach_target(row)
        encoding = data.get("encoding", "terminal_ansi")
        if target is None or data.get("frame_delivery") == "direct" or encoding != "terminal_ansi":
            await super()._handle_terminal_attach(websocket, data)
            return
        assert row is not None and isinstance(terminal_id, str)
        session_manager, config, session_name = target
        registry = self._leases()
        viewer: Literal["web", "gclient"] = "web" if data.get("viewer") == "web" else "gclient"
        record = registry.attach(terminal_id, "proxy", websocket=websocket, viewer=viewer)
        # A tmux client is a typing seat: the newest viewer holds the lease,
        # exactly as every attached desktop client can type.
        displaced = registry.displaced_holder(terminal_id, record.attachment_id)
        control = registry.take_control(terminal_id, record.attachment_id, takeover=True)
        if control.granted and displaced is not None:
            await self._fanout_lease_lost(displaced, record.attachment_id, control.lease_generation)
        cancel_stale_reservations(self, terminal_id, websocket)
        self._tmux_pending[record.attachment_id] = PendingAttachment(
            terminal_id=terminal_id,
            session_name=session_name,
            manager=session_manager,
            config=config,
            owner=websocket,
        )
        await self._send_json(
            websocket,
            {
                "type": "terminal_attach_result",
                "request_id": data.get("request_id"),
                "terminal_id": terminal_id,
                "attachment_id": record.attachment_id,
                "rows": row.rows or 24,
                "cols": row.cols or 80,
                "backend": row.backend,
                "frame_delivery": record.frame_delivery,
                "direct": None,
                "lease_generation": registry.generation(terminal_id),
                "success": True,
            },
        )

    async def _handle_terminal_resize(self, websocket: Any, data: dict[str, Any]) -> None:
        """Resize a tmux client, or activate its pending reservation.

        The first resize is the activation point: it is the earliest message
        carrying the client's real terminal geometry.
        """
        attachment_id = data.get("attachment_id")
        pending = self._tmux_pending.get(attachment_id) if isinstance(attachment_id, str) else None
        bridge = None if pending is not None else await self._tmux_bridge_for(attachment_id)
        if pending is None and bridge is None:
            await super()._handle_terminal_resize(websocket, data)
            return
        assert isinstance(attachment_id, str)
        if pending is not None:
            if pending.owner is not websocket:
                logger.debug(
                    "Ignoring resize for %s from a websocket that does not own it", attachment_id
                )
                return
            if pending.state != STATE_RESERVED:
                # Activation is already in flight; there is no bridge to resize
                # yet, and the client will resend once the terminal is live.
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
        if admitted.applied:
            await self._apply_terminal_sizing(record.terminal_id, admitted.sizing)
        else:
            await self._send_json(
                websocket,
                {
                    "type": "terminal_resize_result",
                    "attachment_id": attachment_id,
                    "applied": False,
                    "owner_viewer": admitted.owner_viewer,
                },
            )
        rows, cols = validate_dimensions(data.get("rows"), data.get("cols"))
        if pending is not None:
            pending.state = STATE_ACTIVATING
            await activate_attachment(self, websocket, attachment_id, pending, rows, cols)
            return
        # The bridge records the geometry tmux runs the client at and repaints
        # nothing for a resize to that same size (#20805).
        resized = await self._tmux_bridge.resize(attachment_id, rows, cols)
        if resized is not None and resized.config is not None:
            try:
                await TmuxSessionManager(resized.config).refresh_client(resized.session_name)
            except Exception as exc:
                logger.debug("Post-resize refresh-client failed: %s", exc)

    async def _handle_terminal_detach(self, websocket: Any, data: dict[str, Any]) -> None:
        attachment_id = data.get("attachment_id")
        if isinstance(attachment_id, str) and (
            attachment_id in self._tmux_pending or await self._tmux_bridge_for(attachment_id)
        ):
            await teardown_bridge(self, attachment_id)
        await super()._handle_terminal_detach(websocket, data)

    async def _handle_terminal_set_viewport(self, websocket: Any, data: dict[str, Any]) -> None:
        """A tmux client has no host viewport; the refresh redraws it instead."""
        bridge = await self._tmux_bridge_for(data.get("attachment_id"))
        if bridge is None:
            await super()._handle_terminal_set_viewport(websocket, data)
            return
        if bridge.config is not None:
            try:
                await TmuxSessionManager(bridge.config).refresh_client(bridge.session_name)
            except Exception as exc:
                logger.debug("refresh-client failed: %s", exc)

"""WebSocket handlers for tmux session management.

TmuxMixin provides handlers for listing, attaching, detaching, creating,
killing, and resizing tmux sessions via PTY relay. Follows the HandlerMixin
pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from websockets.exceptions import ConnectionClosed

from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.servers.websocket.terminal_ws import TerminalWsMixin
from gobby.servers.websocket.tmux_activation import (
    STATE_ACTIVATING,
    STATE_RESERVED,
    PendingAttachment,
    activate_attachment,
    cancel_all_pending,
    cancel_pending_for_owner,
    cancel_stale_reservations,
    teardown_bridge,
    teardown_terminal_bridges,
)
from gobby.terminals.dimensions import InvalidTerminalDimensionsError, validate_dimensions
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.utils.json_helpers import json_dumps
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Default server config (no socket = user's default tmux)
_DEFAULT_CONFIG = TmuxConfig(socket_name="")
_GOBBY_CONFIG = TmuxConfig(socket_name="gobby")


class TmuxMixin(TerminalWsMixin):
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
        async def _send_error(
            self, websocket: Any, message: str, request_id: str | None = None, code: str = "ERROR"
        ) -> None: ...

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
        self._leases().finalize_websocket(websocket, "ws_close")
        hub = getattr(self, "_proxy_hub", None)
        if hub is not None:
            await hub.drop_socket(websocket, "ws_close")
        self._tmux_client_bridges.pop(websocket, None)

    def _get_tmux_manager(self, socket: str) -> TmuxSessionManager:
        """Get the session manager for a given socket."""
        if socket == "gobby":
            return self._tmux_mgr_gobby
        return self._tmux_mgr_default

    def _get_tmux_config(self, socket: str) -> TmuxConfig:
        """Get the config for a given socket.

        The socket templates are fixed: they exist to target the gobby socket
        vs the user's personal default server.
        """
        return _GOBBY_CONFIG if socket == "gobby" else _DEFAULT_CONFIG

    @staticmethod
    def _terminal_context_matches_socket(
        terminal_context: Mapping[str, Any],
        socket: str,
    ) -> bool:
        """Return whether stored terminal context belongs to a UI tmux socket."""
        socket_name = terminal_context.get("tmux_socket_name")
        if isinstance(socket_name, str) and socket_name:
            return socket_name == socket

        socket_path = terminal_context.get("tmux_socket_path")
        if isinstance(socket_path, str) and socket_path:
            recorded_socket = socket_path.rstrip("/").rsplit("/", 1)[-1]
            if socket == "gobby":
                return recorded_socket == "gobby"
            return recorded_socket != "gobby"

        # Legacy contexts without socket metadata came from the default tmux server.
        return socket != "gobby"

    async def _resolve_gobby_session_ids_for_tmux_session(
        self,
        session_name: str,
        socket: str,
        mgr: TmuxSessionManager,
    ) -> list[str]:
        """Resolve active/paused Gobby sessions backed by a tmux session."""
        session_mgr = getattr(self, "session_manager", None)
        if not session_mgr:
            return []

        try:
            pane_ids = {
                tmux_session.pane_id
                for tmux_session in await mgr.list_sessions()
                if tmux_session.name == session_name and tmux_session.pane_id
            }
        except Exception:
            logger.debug("Failed to resolve tmux pane for session kill", exc_info=True)
            return []

        if not pane_ids:
            return []

        matched_session_ids: list[str] = []
        seen: set[str] = set()
        for status in ("active", "paused"):
            try:
                sessions = session_mgr.list(status=status) or []
            except Exception:
                logger.debug("Failed to list %s sessions for tmux kill", status, exc_info=True)
                continue

            for session in sessions:
                terminal_context = getattr(session, "terminal_context", None)
                if not isinstance(terminal_context, dict):
                    continue
                if terminal_context.get("tmux_pane") not in pane_ids:
                    continue
                if not self._terminal_context_matches_socket(terminal_context, socket):
                    continue

                session_id = getattr(session, "id", None)
                if isinstance(session_id, str) and session_id not in seen:
                    matched_session_ids.append(session_id)
                    seen.add(session_id)

        return matched_session_ids

    async def _expire_gobby_sessions_for_tmux_kill(self, session_ids: list[str]) -> list[str]:
        """Expire Gobby sessions whose backing tmux session was killed."""
        session_mgr = getattr(self, "session_manager", None)
        if not session_mgr or not session_ids:
            return []

        from gobby.sessions.activity import clear_trackers

        expired_session_ids: list[str] = []
        for session_id in session_ids:
            try:
                session_mgr.update_status(session_id, "expired")
                clear_trackers(session_id)
                expired_session_ids.append(session_id)
            except Exception:
                logger.warning(
                    "Failed to expire Gobby session %s after tmux kill",
                    session_id,
                    exc_info=True,
                )
                continue

        return expired_session_ids

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
        if target is None or data.get("frame_delivery") == "direct":
            await super()._handle_terminal_attach(websocket, data)
            return
        assert row is not None and isinstance(terminal_id, str)
        session_manager, config, session_name = target
        registry = self._leases()
        record = registry.attach(terminal_id, "proxy", websocket=websocket)
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
        try:
            rows, cols = validate_dimensions(data.get("rows"), data.get("cols"))
        except InvalidTerminalDimensionsError:
            await self._send_json(
                websocket, {"type": "terminal_error", "code": "invalid_dimensions"}
            )
            return
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

    async def _handle_terminal_kill(self, websocket: Any, data: dict[str, Any]) -> None:
        terminal_id = data.get("terminal_id")
        if isinstance(terminal_id, str):
            await teardown_terminal_bridges(self, terminal_id)
        await super()._handle_terminal_kill(websocket, data)

    async def _deliver_operator_write(
        self,
        terminal_id: str,
        attachment_id: str,
        *,
        kind: str,
        payload: str,
        generation: int | None,
        seq: object = None,
    ) -> tuple[str, str | None]:
        """Raw bytes into the tmux client's PTY; everything else goes to the runtime.

        The PTY gives full terminal fidelity (Ctrl+C, arrows, Tab, mouse), which
        ``send-keys -l`` cannot carry.
        """
        bridge_fd = await self._tmux_bridge.get_master_fd(attachment_id)
        if bridge_fd is None:
            return await super()._deliver_operator_write(
                terminal_id,
                attachment_id,
                kind=kind,
                payload=payload,
                generation=generation,
                seq=seq,
            )
        try:
            await asyncio.to_thread(os.write, bridge_fd, payload.encode("utf-8"))
        except OSError as exc:
            logger.warning("Failed to write to tmux bridge %s: %s", attachment_id, exc)
            return "indeterminate", "indeterminate_backend"
        return "delivered", None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_tmux_list_sessions(self, websocket: Any, data: dict[str, Any]) -> None:
        """List tmux sessions from both default and gobby servers."""
        request_id = data.get("request_id")

        sessions: list[dict[str, Any]] = []

        # Get active agent runs from DB for agent-managed session detection
        active_runs: list[Any] = []
        session_mgr = getattr(self, "session_manager", None)
        if session_mgr:
            try:
                from gobby.storage.agents import LocalAgentRunManager

                active_runs = LocalAgentRunManager(session_mgr.db).list_active_for_machine(
                    require_machine_id()
                )
            except Exception:
                logger.debug("Failed to load active agent runs", exc_info=True)

        # Build tmux_pane -> (session_title, gobby_session_id) map from active Gobby sessions
        # and collect IDs of sessions whose tmux pane is still alive.
        # Uses tmux_pane (e.g. "%64") which is stable, unlike parent_pid which
        # goes stale when the CLI process exits and the shell reclaims the pane.
        pane_to_title: dict[str, str] = {}
        pane_to_session_id: dict[str, str] = {}
        live_cli_session_ids: list[str] = []

        # Collect all live pane IDs from the user's default tmux server
        live_pane_ids: set[str] = set()
        try:
            live_pane_ids = await self._tmux_mgr_default.list_pane_ids()
        except Exception:
            logger.debug("Failed to list live tmux panes", exc_info=True)

        session_mgr = getattr(self, "session_manager", None)
        if session_mgr:
            try:
                # Check both active and paused sessions (mirrors frontend filter)
                for status in ("active", "paused"):
                    for gs in session_mgr.list(status=status):
                        if gs.terminal_context:
                            tmux_pane = gs.terminal_context.get("tmux_pane")
                            if tmux_pane:
                                if gs.title:
                                    pane_to_title[tmux_pane] = gs.title
                                pane_to_session_id[tmux_pane] = gs.id
                                if tmux_pane in live_pane_ids:
                                    live_cli_session_ids.append(gs.id)
            except Exception:
                logger.debug("Failed to build pane-to-title map", exc_info=True)

        for socket_name, mgr in [
            ("default", self._tmux_mgr_default),
            ("gobby", self._tmux_mgr_gobby),
        ]:
            try:
                tmux_sessions = await mgr.list_sessions()
                for s in tmux_sessions:
                    # Check if this session is managed by an agent
                    agent_managed = False
                    agent_run_id = None
                    for run in active_runs:
                        row = None
                        manager = getattr(self, "terminal_manager", None)
                        if manager is not None and run.terminal_id:
                            row = manager.get(run.terminal_id)
                        if (row.session_name if row is not None else None) == s.name:
                            agent_managed = True
                            agent_run_id = run.id
                            break

                    attached_bridge = None

                    # Look up synthesized session title and gobby session ID via tmux pane ID
                    pane_id = getattr(s, "pane_id", None)
                    session_title = pane_to_title.get(pane_id) if pane_id else None
                    gobby_session_id = pane_to_session_id.get(pane_id) if pane_id else None

                    sessions.append(
                        {
                            "name": s.name,
                            "socket": socket_name,
                            "pane_pid": s.pane_pid,
                            "pane_dead": getattr(s, "pane_dead", False),
                            "pane_title": s.pane_title,
                            "pane_command": s.pane_command,
                            "pane_path": s.pane_path,
                            "window_name": s.window_name,
                            "session_title": session_title,
                            "gobby_session_id": gobby_session_id,
                            "agent_managed": agent_managed,
                            "agent_run_id": agent_run_id,
                            "attached_bridge": attached_bridge,
                        }
                    )
            except Exception as e:
                logger.warning("Failed to list %s tmux sessions: %s", socket_name, e)

        response: dict[str, Any] = {
            "type": "terminal_list",
            "sessions": sessions,
            "live_cli_session_ids": live_cli_session_ids,
        }
        if request_id:
            response["request_id"] = request_id

        try:
            await websocket.send(json_dumps(response))
        except ConnectionClosed:
            logger.debug("Client disconnected before tmux session list response was sent")

    async def _handle_tmux_attach(self, websocket: Any, data: dict[str, Any]) -> None:
        """Attach to a tmux session through the host proxy, never a second tmux client."""
        request_id = data.get("request_id")
        session_name = data.get("session_name")
        socket = "gobby" if data.get("socket", "default") == "gobby" else "default"

        if not session_name:
            await self._send_error(websocket, "Missing session_name", request_id=request_id)
            return

        mgr = self._get_tmux_manager(socket)
        if not await mgr.has_session(session_name):
            await self._send_error(
                websocket, f"Session '{session_name}' not found", request_id=request_id
            )
            return

        manager = getattr(self, "terminal_manager", None)
        terminal_id = None
        if manager is not None:
            try:
                for row in manager.list_by_project(self._project_id(websocket) or ""):
                    if row.session_name == session_name:
                        terminal_id = row.id
                        break
            except Exception:
                logger.debug("session_name lookup failed", exc_info=True)
        if terminal_id is not None:
            await self._handle_terminal_attach(
                websocket,
                {
                    "request_id": request_id,
                    "terminal_id": terminal_id,
                    "frame_delivery": "proxy",
                },
            )
            return

        streaming_id = f"tmux-{uuid4().hex[:12]}"
        self._tmux_client_bridges.setdefault(websocket, set()).add(streaming_id)
        response: dict[str, Any] = {
            "type": "terminal_attach_result",
            "success": True,
            "streaming_id": streaming_id,
            "session_name": session_name,
            "socket": socket,
        }
        if request_id:
            response["request_id"] = request_id
        try:
            await websocket.send(json_dumps(response))
        except ConnectionClosed:
            logger.debug("Client disconnected before tmux attach response was sent")

    async def _handle_tmux_detach(self, websocket: Any, data: dict[str, Any]) -> None:
        """Detach a host-proxy attachment (legacy streaming_id still accepted)."""
        request_id = data.get("request_id")
        streaming_id = data.get("streaming_id") or data.get("attachment_id")

        if not streaming_id:
            await self._send_error(websocket, "Missing streaming_id", request_id=request_id)
            return

        if isinstance(streaming_id, str) and streaming_id in self._proxy().attachments:
            await self._proxy().finalize_attachment(streaming_id, "detach")

        client_bridges = self._tmux_client_bridges.get(websocket)
        if client_bridges:
            client_bridges.discard(streaming_id)

        response: dict[str, Any] = {
            "type": "terminal_detach_result",
            "success": True,
            "streaming_id": streaming_id,
        }
        if request_id:
            response["request_id"] = request_id

        await websocket.send(json_dumps(response))

    async def _handle_tmux_create_session(self, websocket: Any, data: dict[str, Any]) -> None:
        """Create a new tmux session."""
        request_id = data.get("request_id")
        name = data.get("name")
        command = data.get("command")
        cwd = data.get("cwd")
        socket = data.get("socket", "default")

        mgr = self._get_tmux_manager(socket)

        if not mgr.is_available():
            await self._send_error(websocket, "tmux is not installed", request_id=request_id)
            return

        try:
            session_name = name or f"web-{uuid4().hex[:8]}"
            info = await mgr.create_session(
                name=session_name,
                command=command,
                cwd=cwd,
            )

            # Broadcast session event
            await self._broadcast_tmux_event("session_created", info.name, socket)

            response: dict[str, Any] = {
                "type": "terminal_create_result",
                "success": True,
                "session_name": info.name,
                "pane_pid": info.pane_pid,
                "socket": socket,
            }
            if request_id:
                response["request_id"] = request_id

            await websocket.send(json_dumps(response))

        except Exception as e:
            logger.error("Failed to create tmux session: %s", e)
            await self._send_error(websocket, f"Create failed: {e}", request_id=request_id)

    async def _handle_tmux_kill_session(self, websocket: Any, data: dict[str, Any]) -> None:
        """Kill a tmux session."""
        request_id = data.get("request_id")
        session_name = data.get("session_name")
        socket = data.get("socket", "default")

        if not session_name:
            await self._send_error(websocket, "Missing session_name", request_id=request_id)
            return

        # Refuse to kill agent-managed sessions
        session_mgr = getattr(self, "session_manager", None)
        if session_mgr:
            try:
                from gobby.storage.agents import LocalAgentRunManager

                for run in LocalAgentRunManager(session_mgr.db).list_active_for_machine(
                    require_machine_id()
                ):
                    row = None
                    manager = getattr(self, "terminal_manager", None)
                    if manager is not None and run.terminal_id:
                        row = manager.get(run.terminal_id)
                    if (row.session_name if row is not None else None) == session_name:
                        await self._send_error(
                            websocket,
                            f"Session '{session_name}' is managed by agent {run.id}",
                            request_id=request_id,
                            code="AGENT_MANAGED",
                        )
                        return
            except Exception:
                logger.debug("Failed to check agent-managed sessions", exc_info=True)

        mgr = self._get_tmux_manager(socket)
        gobby_session_ids = await self._resolve_gobby_session_ids_for_tmux_session(
            session_name,
            socket,
            mgr,
        )

        try:
            success = await mgr.destroy_session(session_name)
            expired_session_ids: list[str] = []
            if success:
                for attachment_id, bridge in (await self._tmux_bridge.list_bridges()).items():
                    bridge_socket = "default"
                    if bridge.config is not None and bridge.config.socket_name == "gobby":
                        bridge_socket = "gobby"
                    if bridge.session_name == session_name and bridge_socket == socket:
                        await teardown_bridge(self, attachment_id)
                expired_session_ids = await self._expire_gobby_sessions_for_tmux_kill(
                    gobby_session_ids
                )
                await self._broadcast_tmux_event("session_killed", session_name, socket)

            response: dict[str, Any] = {
                "type": "terminal_kill_result",
                "success": success,
                "session_name": session_name,
                "expired_session_ids": expired_session_ids,
            }
            if request_id:
                response["request_id"] = request_id

            await websocket.send(json_dumps(response))

        except Exception as e:
            logger.error("Failed to kill tmux session '%s': %s", session_name, e)
            await self._send_error(websocket, f"Kill failed: {e}", request_id=request_id)

    async def _handle_tmux_resize(self, websocket: Any, data: dict[str, Any]) -> None:
        """Lease-gated resize through TerminalRuntime; no second tmux client."""
        streaming_id = data.get("streaming_id") or data.get("attachment_id")
        if not streaming_id:
            return
        payload = dict(data)
        payload["attachment_id"] = streaming_id
        await self._handle_terminal_resize(websocket, payload)

    async def _handle_tmux_refresh_client(self, websocket: Any, data: dict[str, Any]) -> None:
        """Force tmux to redraw the clients attached to a session."""
        request_id = data.get("request_id")
        session_name = data.get("session_name")
        socket = data.get("socket", "default")

        if not session_name:
            await self._send_error(websocket, "Missing session_name", request_id=request_id)
            return

        manager = self._get_tmux_manager(socket)
        try:
            if not await manager.has_session(session_name):
                await self._send_error(
                    websocket,
                    f"Session '{session_name}' not found",
                    request_id=request_id,
                )
                return
            await manager.refresh_client(session_name)
        except Exception as e:
            logger.error("Failed to refresh tmux session '%s': %s", session_name, e)
            await self._send_error(websocket, f"Refresh failed: {e}", request_id=request_id)
            return

        response: dict[str, Any] = {
            "type": "terminal_refresh_result",
            "success": True,
            "session_name": session_name,
            "socket": socket,
        }
        if request_id:
            response["request_id"] = request_id
        await websocket.send(json_dumps(response))

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast_tmux_event(self, event: str, session_name: str, socket: str) -> None:
        """Broadcast a tmux session lifecycle event to subscribed clients."""
        if not self.clients:
            return

        from datetime import UTC, datetime

        from websockets.exceptions import ConnectionClosed

        message = json_dumps(
            {
                "type": "terminal_event",
                "event": event,
                "session_name": session_name,
                "socket": socket,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        for ws in list(self.clients.keys()):
            try:
                subs = getattr(ws, "subscriptions", None)
                if subs is not None:
                    if "terminal_event" not in subs and "*" not in subs:
                        continue
                await ws.send(message)
            except ConnectionClosed:
                pass
            except Exception as e:
                logger.warning("Tmux event broadcast failed: %s", e)

"""WebSocket handlers for tmux session management.

TmuxMixin provides handlers for listing, attaching, detaching, creating,
killing, and resizing tmux sessions via PTY relay. Follows the HandlerMixin
pattern.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from uuid import uuid4

from websockets.exceptions import ConnectionClosed

from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

logger = logging.getLogger(__name__)

# Default server config (no socket = user's default tmux)
_DEFAULT_CONFIG = TmuxConfig(socket_name="")
_GOBBY_CONFIG = TmuxConfig(socket_name="gobby")


class TmuxMixin:
    """Mixin providing tmux session management handlers for WebSocketServer.

    Requires on the host class:
    - ``self.clients: dict[Any, dict[str, Any]]``
    - ``async self.broadcast_terminal_output(run_id, data)`` (from BroadcastMixin)
    - ``async self._send_error(websocket, message, ...)`` (from HandlerMixin)
    """

    clients: dict[Any, dict[str, Any]]

    # These are provided by other mixins (BroadcastMixin, HandlerMixin).
    # Declared as TYPE_CHECKING-only protocol hints to avoid shadowing real methods.
    if TYPE_CHECKING:

        async def broadcast_terminal_output(self, run_id: str, data: str) -> None: ...
        async def _send_error(
            self, websocket: Any, message: str, request_id: str | None = None, code: str = "ERROR"
        ) -> None: ...

    def _init_tmux(self) -> None:
        """Initialize tmux subsystem. Call from WebSocketServer.__init__."""
        self._tmux_bridge = TmuxPTYBridge()
        self._tmux_mgr_gobby = TmuxSessionManager(_GOBBY_CONFIG)
        self._tmux_mgr_default = TmuxSessionManager(_DEFAULT_CONFIG)
        # Track which client owns which bridge (for cleanup on disconnect)
        self._tmux_client_bridges: dict[Any, set[str]] = {}  # websocket -> {streaming_id}

    async def _cleanup_tmux(self) -> None:
        """Clean up all tmux bridges. Call from WebSocketServer.stop."""
        from gobby.agents.pty_reader import get_pty_reader_manager

        reader = get_pty_reader_manager()
        for streaming_id in list((await self._tmux_bridge.list_bridges()).keys()):
            await reader.stop_reader(streaming_id)
            await self._tmux_bridge.detach(streaming_id)
        self._tmux_client_bridges.clear()

    async def _cleanup_tmux_client(self, websocket: Any) -> None:
        """Clean up bridges owned by a disconnecting client."""
        from gobby.agents.pty_reader import get_pty_reader_manager

        bridge_ids = self._tmux_client_bridges.pop(websocket, set())
        if not bridge_ids:
            return

        reader = get_pty_reader_manager()
        for streaming_id in bridge_ids:
            await reader.stop_reader(streaming_id)
            await self._tmux_bridge.detach(streaming_id)
            logger.debug(f"Cleaned up tmux bridge {streaming_id} for disconnected client")

    def _get_tmux_manager(self, socket: str) -> TmuxSessionManager:
        """Get the session manager for a given socket."""
        if socket == "gobby":
            return self._tmux_mgr_gobby
        return self._tmux_mgr_default

    def _get_tmux_config(self, socket: str) -> TmuxConfig:
        """Get the config for a given socket."""
        if socket == "gobby":
            return _GOBBY_CONFIG
        return _DEFAULT_CONFIG

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

        from gobby.servers.routes.sessions.statusline_activity import clear_trackers

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

                active_runs = LocalAgentRunManager(session_mgr.db).list_active()
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
                        if run.tmux_session_name == s.name:
                            agent_managed = True
                            agent_run_id = run.id
                            break

                    # Check if a bridge is active for this session
                    attached_bridge = None
                    bridges = await self._tmux_bridge.list_bridges()
                    for sid, bridge in bridges.items():
                        if (
                            bridge.session_name == s.name
                            and bridge.socket_name == mgr.config.socket_name
                        ):
                            attached_bridge = sid
                            break

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
                            "window_name": s.window_name,
                            "session_title": session_title,
                            "gobby_session_id": gobby_session_id,
                            "agent_managed": agent_managed,
                            "agent_run_id": agent_run_id,
                            "attached_bridge": attached_bridge,
                        }
                    )
            except Exception as e:
                logger.warning(f"Failed to list {socket_name} tmux sessions: {e}")

        response: dict[str, Any] = {
            "type": "tmux_sessions_list",
            "sessions": sessions,
            "live_cli_session_ids": live_cli_session_ids,
        }
        if request_id:
            response["request_id"] = request_id

        try:
            await websocket.send(json.dumps(response))
        except ConnectionClosed:
            logger.debug("Client disconnected before tmux session list response was sent")

    async def _handle_tmux_attach(self, websocket: Any, data: dict[str, Any]) -> None:
        """Attach to a tmux session via PTY relay."""
        from gobby.agents.pty_reader import get_pty_reader_manager

        request_id = data.get("request_id")
        session_name = data.get("session_name")
        socket = data.get("socket", "default")

        if not session_name:
            await self._send_error(websocket, "Missing session_name", request_id=request_id)
            return

        config = self._get_tmux_config(socket)
        mgr = self._get_tmux_manager(socket)

        # Verify session exists
        if not await mgr.has_session(session_name):
            await self._send_error(
                websocket, f"Session '{session_name}' not found", request_id=request_id
            )
            return

        streaming_id = f"tmux-{uuid4().hex[:12]}"

        try:
            master_fd = await self._tmux_bridge.attach(
                session_name=session_name,
                streaming_id=streaming_id,
                config=config,
            )

            # Hide tmux status bar and force redraw BEFORE starting the PTY reader.
            # This prevents the reader from capturing status-bar output before it's
            # disabled, which would corrupt the initial terminal state.
            try:
                args: list[str] = [config.command]
                if config.socket_name:
                    args.extend(["-L", config.socket_name])

                # Turn off status bar
                status_args = args + ["set-option", "-t", session_name, "status", "off"]
                proc = await asyncio.create_subprocess_exec(
                    *status_args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5.0)

                # Enable mouse mode so scroll wheel works in the web UI
                mouse_args = args + ["set-option", "-t", session_name, "mouse", "on"]
                proc_mouse = await asyncio.create_subprocess_exec(
                    *mouse_args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc_mouse.wait(), timeout=5.0)

                # Force redraw clients attached to this session
                refresh_args = args + ["refresh-client", "-t", session_name]
                proc2 = await asyncio.create_subprocess_exec(
                    *refresh_args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc2.wait(), timeout=5.0)
            except Exception as e:
                logger.debug(f"Failed to configure/refresh tmux session: {e}")

            # NOW start PTY reader — tmux state is clean
            reader = get_pty_reader_manager()

            # Create a lightweight RunningAgent-like object for the reader
            class _BridgeAgent(NamedTuple):
                run_id: str
                master_fd: int

            await reader.start_reader(cast(Any, _BridgeAgent(streaming_id, master_fd)))

            # Track ownership for cleanup
            if websocket not in self._tmux_client_bridges:
                self._tmux_client_bridges[websocket] = set()
            self._tmux_client_bridges[websocket].add(streaming_id)

            response: dict[str, Any] = {
                "type": "tmux_attach_result",
                "success": True,
                "streaming_id": streaming_id,
                "session_name": session_name,
                "socket": socket,
            }
            if request_id:
                response["request_id"] = request_id

            await websocket.send(json.dumps(response))

        except Exception as e:
            logger.error(f"Failed to attach tmux session '{session_name}': {e}")
            await self._send_error(websocket, f"Attach failed: {e}", request_id=request_id)

    async def _handle_tmux_detach(self, websocket: Any, data: dict[str, Any]) -> None:
        """Detach from a tmux session (close PTY bridge)."""
        from gobby.agents.pty_reader import get_pty_reader_manager

        request_id = data.get("request_id")
        streaming_id = data.get("streaming_id")

        if not streaming_id:
            await self._send_error(websocket, "Missing streaming_id", request_id=request_id)
            return

        reader = get_pty_reader_manager()
        await reader.stop_reader(streaming_id)
        await self._tmux_bridge.detach(streaming_id)

        # Remove from client tracking
        client_bridges = self._tmux_client_bridges.get(websocket)
        if client_bridges:
            client_bridges.discard(streaming_id)

        response: dict[str, Any] = {
            "type": "tmux_detach_result",
            "success": True,
            "streaming_id": streaming_id,
        }
        if request_id:
            response["request_id"] = request_id

        await websocket.send(json.dumps(response))

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
                "type": "tmux_create_result",
                "success": True,
                "session_name": info.name,
                "pane_pid": info.pane_pid,
                "socket": socket,
            }
            if request_id:
                response["request_id"] = request_id

            await websocket.send(json.dumps(response))

        except Exception as e:
            logger.error(f"Failed to create tmux session: {e}")
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

                for run in LocalAgentRunManager(session_mgr.db).list_active():
                    if run.tmux_session_name == session_name:
                        await self._send_error(
                            websocket,
                            f"Session '{session_name}' is managed by agent {run.id}",
                            request_id=request_id,
                            code="AGENT_MANAGED",
                        )
                        return
            except Exception:
                logger.debug("Failed to check agent-managed sessions", exc_info=True)

        # Detach any bridges to this session first
        from gobby.agents.pty_reader import get_pty_reader_manager

        reader = get_pty_reader_manager()
        mgr = self._get_tmux_manager(socket)
        gobby_session_ids = await self._resolve_gobby_session_ids_for_tmux_session(
            session_name,
            socket,
            mgr,
        )

        bridges = await self._tmux_bridge.list_bridges()
        for sid, bridge in list(bridges.items()):
            if bridge.session_name == session_name and bridge.socket_name == mgr.config.socket_name:
                await reader.stop_reader(sid)
                await self._tmux_bridge.detach(sid)
                # Clean from all client tracking
                for client_set in self._tmux_client_bridges.values():
                    client_set.discard(sid)

        try:
            success = await mgr.kill_session(session_name)
            expired_session_ids: list[str] = []
            if success:
                expired_session_ids = await self._expire_gobby_sessions_for_tmux_kill(
                    gobby_session_ids
                )
                await self._broadcast_tmux_event("session_killed", session_name, socket)

            response: dict[str, Any] = {
                "type": "tmux_kill_result",
                "success": success,
                "session_name": session_name,
                "expired_session_ids": expired_session_ids,
            }
            if request_id:
                response["request_id"] = request_id

            await websocket.send(json.dumps(response))

        except Exception as e:
            logger.error(f"Failed to kill tmux session '{session_name}': {e}")
            await self._send_error(websocket, f"Kill failed: {e}", request_id=request_id)

    async def _handle_tmux_resize(self, websocket: Any, data: dict[str, Any]) -> None:
        """Resize PTY for an attached tmux session."""
        streaming_id = data.get("streaming_id")
        rows = data.get("rows")
        cols = data.get("cols")

        if not streaming_id or not rows or not cols:
            return  # Silent failure for resize events

        bridge = await self._tmux_bridge.resize(streaming_id, int(rows), int(cols))

        # After resizing the PTY, tell tmux to redraw at the new dimensions
        if bridge:
            try:
                config = self._get_tmux_config(
                    "gobby" if bridge.socket_name == "gobby" else "default"
                )
                args: list[str] = [config.command]
                if config.socket_name:
                    args.extend(["-L", config.socket_name])
                refresh_args = args + ["refresh-client", "-t", bridge.session_name]
                proc = await asyncio.create_subprocess_exec(
                    *refresh_args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception as e:
                logger.debug(f"Post-resize refresh-client failed: {e}")

    async def _handle_tmux_refresh_client(self, websocket: Any, data: dict[str, Any]) -> None:
        """Force tmux to redraw the clients attached to a session."""
        session_name = data.get("session_name")
        socket = data.get("socket", "default")

        if not session_name:
            return

        config = self._get_tmux_config(socket)
        try:
            args: list[str] = [config.command]
            if config.socket_name:
                args.extend(["-L", config.socket_name])

            refresh_args = args + ["refresh-client", "-t", session_name]
            proc = await asyncio.create_subprocess_exec(
                *refresh_args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception as e:
            logger.debug(f"Failed to refresh tmux session: {e}")

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast_tmux_event(self, event: str, session_name: str, socket: str) -> None:
        """Broadcast a tmux session lifecycle event to subscribed clients."""
        if not self.clients:
            return

        from datetime import UTC, datetime

        from websockets.exceptions import ConnectionClosed

        message = json.dumps(
            {
                "type": "tmux_session_event",
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
                    if "tmux_session_event" not in subs and "*" not in subs:
                        continue
                await ws.send(message)
            except ConnectionClosed:
                pass
            except Exception as e:
                logger.warning(f"Tmux event broadcast failed: {e}")

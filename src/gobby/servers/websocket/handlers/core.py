"""WebSocket message handlers.

HandlerMixin provides individual message type handlers for WebSocketServer.
Extracted from server.py as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.utils.json_helpers import json_dumps

logger = logging.getLogger(__name__)


class HandlerMixin:
    """Mixin providing message handler methods for WebSocketServer.

    Requires on the host class:
    - ``self.mcp_manager: MCPClientManager``
    - ``self.stop_registry: Any``
    - ``self.broadcast_autonomous_event(...)`` (from BroadcastMixin)
    """

    mcp_manager: MCPClientManager
    stop_registry: Any

    async def broadcast_autonomous_event(
        self, event: str, session_id: str, **kwargs: Any
    ) -> None: ...

    async def _send_error(
        self,
        websocket: Any,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None:
        """
        Send error message to client.

        Args:
            websocket: Client WebSocket connection
            message: Error message
            request_id: Optional request ID for correlation
            code: Error code (default: "ERROR")
        """
        error_msg: dict[str, Any] = {
            "type": "error",
            "code": code,
            "message": message,
        }

        if request_id:
            error_msg["request_id"] = request_id

        await websocket.send(json_dumps(error_msg))

    async def _call_external_mcp(self, mcp_name: str, tool_name: str, args: Any) -> Any:
        from gobby.mcp_proxy.services.server_resolution import as_project_id, resolved_server_id

        # Session-bound calls carry the session's effective project; the
        # sessionless path is explicitly GLOBAL (as_project_id's default).
        project_id = as_project_id(getattr(self, "project_id", None))
        server_id = resolved_server_id(self.mcp_manager, mcp_name, project_id=project_id)
        if server_id is None:
            return {
                "success": False,
                "error": f"Server '{mcp_name}' not found in project scope {project_id}",
                "error_code": "SERVER_NOT_FOUND",
                "server_name": mcp_name,
                "tool_name": tool_name,
            }
        return await self.mcp_manager.call_tool(server_id, tool_name=tool_name, arguments=args)

    async def _handle_tool_call(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle tool_call message and route to MCP server.

        Message format:
        {
            "type": "tool_call",
            "request_id": "uuid",
            "mcp": "memory",
            "tool": "add_messages",
            "args": {...}
        }

        Response format:
        {
            "type": "tool_result",
            "request_id": "uuid",
            "result": {...}
        }

        Args:
            websocket: Client WebSocket connection
            data: Parsed tool call message
        """
        request_id = data.get("request_id")
        mcp_name = data.get("mcp")
        tool_name = data.get("tool")
        args = data.get("args", {})

        if (
            not isinstance(request_id, str)
            or not isinstance(mcp_name, str)
            or not isinstance(tool_name, str)
        ):
            await self._send_error(
                websocket,
                "Missing or invalid required fields: request_id, mcp, tool (must be strings)",
                request_id=str(request_id) if request_id else None,
            )
            return

        try:
            # Route to internal registries first, then external MCP
            internal_mgr = getattr(self, "internal_manager", None)
            if internal_mgr and internal_mgr.is_internal(mcp_name):
                registry = internal_mgr.get_registry(mcp_name)
                if registry:
                    try:
                        result = await registry.call(tool_name, args)
                    except ValueError as e:
                        logger.debug("Registry miss for %s, falling back to MCP: %s", tool_name, e)
                        result = await self._call_external_mcp(mcp_name, tool_name, args)
                else:
                    result = await self._call_external_mcp(mcp_name, tool_name, args)
            else:
                result = await self._call_external_mcp(mcp_name, tool_name, args)

            # Send result back to client
            await websocket.send(
                json_dumps(
                    {
                        "type": "tool_result",
                        "request_id": request_id,
                        "result": result,
                    }
                )
            )

        except ValueError as e:
            # Unknown MCP server
            await self._send_error(websocket, str(e), request_id=request_id)

        except Exception as e:
            logger.exception("Tool call error: %s.%s", mcp_name, tool_name)
            await self._send_error(websocket, f"Tool call failed: {str(e)}", request_id=request_id)

    async def _handle_ping(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle manual ping message for latency measurement.

        Sends pong response with latency value.

        Args:
            websocket: Client WebSocket connection
            data: Ping message (ignored)
        """
        await websocket.send(
            json_dumps(
                {
                    "type": "pong",
                    "latency": getattr(websocket, "latency", 0.0),
                }
            )
        )

    async def _handle_subscribe(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle subscribe message to register for specific events.

        Args:
            websocket: Client WebSocket connection
            data: Subscribe message with "events" list
        """
        events = data.get("events", [])
        if not isinstance(events, list):
            await self._send_error(websocket, "events must be a list of strings")
            return

        if not hasattr(websocket, "subscriptions") or websocket.subscriptions is None:
            websocket.subscriptions = set()

        websocket.subscriptions.update(events)
        logger.debug("Client %s subscribed to: %s", websocket.user_id, events)

        await websocket.send(
            json_dumps(
                {
                    "type": "subscribe_success",
                    "events": list(websocket.subscriptions),
                }
            )
        )

    async def _handle_unsubscribe(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle unsubscribe message to unregister from specific events.

        Args:
            websocket: Client WebSocket connection
            data: Unsubscribe message with "events" list
        """
        events = data.get("events", [])
        if not isinstance(events, list):
            await self._send_error(websocket, "events must be a list of strings")
            return

        current_subscriptions: set[str] = getattr(websocket, "subscriptions", set())

        # If events list is empty or contains "*", unsubscribe from all
        if not events or "*" in events:
            current_subscriptions.clear()
        else:
            for event in events:
                current_subscriptions.discard(event)

        logger.debug("Client %s unsubscribed from: %s", websocket.user_id, events)

        await websocket.send(
            json_dumps(
                {
                    "type": "unsubscribe_success",
                    "events": list(current_subscriptions),
                }
            )
        )

    async def _handle_stop_request(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle stop_request message to signal a session to stop.

        Message format:
        {
            "type": "stop_request",
            "session_id": "uuid",
            "reason": "optional reason string"
        }

        Response format:
        {
            "type": "stop_response",
            "session_id": "uuid",
            "success": true,
            "signaled_at": "iso8601"
        }

        Args:
            websocket: Client WebSocket connection
            data: Parsed stop request message
        """
        session_id = data.get("session_id")
        reason = data.get("reason", "WebSocket stop request")

        if not session_id:
            await self._send_error(websocket, "Missing required field: session_id")
            return

        if not self.stop_registry:
            await self._send_error(websocket, "Stop registry not available", code="UNAVAILABLE")
            return

        try:
            # Signal the stop
            signal = self.stop_registry.signal_stop(
                session_id=session_id,
                reason=reason,
                source="websocket",
            )

            # Send acknowledgment
            await websocket.send(
                json_dumps(
                    {
                        "type": "stop_response",
                        "session_id": session_id,
                        "success": True,
                        "signaled_at": signal.requested_at.isoformat(),
                    }
                )
            )

            # Broadcast the stop_requested event to all clients
            await self.broadcast_autonomous_event(
                event="stop_requested",
                session_id=session_id,
                reason=reason,
                source="websocket",
            )

            logger.info("Stop requested for session %s via WebSocket", session_id)
        except Exception as e:
            logger.error("Error handling stop request: %s", e)
            await self._send_error(websocket, f"Failed to signal stop: {str(e)}")

    async def _handle_terminal_input(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle terminal input for a running agent.

        Message format:
        {
            "type": "terminal_input",
            "run_id": "uuid",
            "data": "raw input string"
        }

        Args:
            websocket: Client WebSocket connection
            data: Parsed terminal input message
        """
        run_id = data.get("terminal_id") or data.get("run_id")
        input_data = data.get("data")

        if not run_id or input_data is None:
            # Don't send error for every keystroke if malformed, just log debug
            logger.debug(
                "Invalid terminal_input: run_id=%s, data_len=%s",
                run_id,
                len(str(input_data)) if input_data else 0,
            )
            return

        if not isinstance(input_data, str):
            # input_data must be a string to encode; log and skip non-strings
            logger.debug(
                "Invalid terminal_input type: run_id=%s, data_type=%s",
                run_id,
                type(input_data).__name__,
            )
            return

        # The runs table keys on a uuid column -- an id that cannot be one
        # raises instead of missing. The web terminal answers tmux's DA and DSR
        # queries as terminal_input, so a reply that lands after its attachment
        # detached arrives here carrying a tmux streaming id, which is exactly
        # such an id (#20803).
        try:
            uuid.UUID(run_id)
        except ValueError:
            logger.debug("Ignoring terminal_input for non-agent id %s", run_id)
            return

        from gobby.storage.agents import LocalAgentRunManager
        from gobby.storage.terminals import TerminalManager
        from gobby.terminals.write_coordinator import WriteRequest

        db = getattr(self, "_db", None) or getattr(
            getattr(self, "session_manager", None), "db", None
        )
        if not db:
            logger.warning("No database available to look up agent %s", run_id)
            return

        run = LocalAgentRunManager(db).get(run_id)
        if not run:
            return

        coordinator = getattr(self, "write_coordinator", None) or getattr(
            getattr(self, "terminal_services", None), "coordinator", None
        )
        manager = getattr(self, "terminal_manager", None)
        if manager is None:
            manager = TerminalManager(db)
        if not run.terminal_id:
            logger.warning("Agent %s has no terminal - cannot route input", run_id)
            return
        terminal = manager.get(run.terminal_id)
        if terminal is None or coordinator is None:
            logger.warning("Agent %s has no writable terminal - cannot route input", run_id)
            return
        try:
            await coordinator.write(
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=f"ws-input:{run_id}",
                    origin="automatic",
                    kind="text",
                    payload=input_data,
                )
            )
        except (OSError, RuntimeError) as e:
            logger.warning("Failed to send keys to agent %s: %s", run_id, e)

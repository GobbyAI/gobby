"""WebSocket broadcast methods.

BroadcastMixin provides all broadcast_* methods for WebSocketServer.
Extracted from server.py as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class BroadcastMixin:
    """Mixin providing broadcast methods for WebSocketServer.

    Requires ``self.clients: dict[Any, dict[str, Any]]`` on the host class.
    """

    clients: dict[Any, dict[str, Any]]

    def _is_subscribed(self, websocket: Any, message: dict[str, Any]) -> bool:
        """Check if a client is subscribed to receive a message."""
        # Clients without subscriptions receive nothing
        subs = getattr(websocket, "subscriptions", None)
        if subs is None:
            return False

        # Global wildcard subscription
        if "*" in subs:
            return True

        msg_type = message.get("type")

        # High-volume event types require explicit subscription
        event_types = {
            "hook_event",
            "session_message",
            "session_event",
            "agent_event",
            "agent_message",
            "agent_command",
            "worktree_event",
            "autonomous_event",
            "pipeline_event",
            "terminal_output",
            "tmux_session_event",
            "canvas_event",
            "skill_event",
            "mcp_event",
            "workflow_event",
            "project_event",
            "cron_event",
        }

        # Non-event messages pass through for any subscribed client
        if msg_type not in event_types:
            return True

        # Check for message type subscription
        if msg_type in subs:
            return True

        # Parametric subscriptions: "type:key=value"
        # e.g., "session_message:session_id=abc123" matches session_message
        # events where the session_id field equals "abc123".
        for sub in subs:
            if ":" not in sub:
                continue
            sub_type, param_str = sub.split(":", 1)
            if sub_type != msg_type or "=" not in param_str:
                continue
            key, value = param_str.split("=", 1)
            if message.get(key) == value:
                return True

        # Special casing for hook_event granularity (subscribe by event_type)
        if msg_type == "hook_event":
            event_type = message.get("event_type")
            if event_type and event_type in subs:
                return True

        return False

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Broadcast message to all connected clients.

        Filters messages based on client subscriptions using _is_subscribed.

        Args:
            message: Dictionary to serialize and send
        """
        if not self.clients:
            return

        message_str = json.dumps(message)
        sent_count = 0
        failed_count = 0

        for websocket in list(self.clients.keys()):
            try:
                if not self._is_subscribed(websocket, message):
                    continue

                await websocket.send(message_str)
                sent_count += 1
            except ConnectionClosed:
                # Client disconnecting, will be cleaned up in handler
                failed_count += 1
            except Exception as e:
                logger.warning(f"Broadcast failed for client: {e}")
                failed_count += 1

        if sent_count > 0 or failed_count > 0:
            logger.debug(
                f"Broadcast {message.get('type')}: {sent_count} sent, {failed_count} failed"
            )

    async def broadcast_session_event(
        self,
        event: str,
        session_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast session event (created, updated, ended)."""
        message = {
            "type": "session_event",
            "event": event,
            "session_id": session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_skill_event(
        self,
        event: str,
        skill_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast skill event (created, updated, deleted, bulk_changed)."""
        message = {
            "type": "skill_event",
            "event": event,
            "skill_id": skill_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_mcp_event(
        self,
        event: str,
        server_name: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast MCP server event (added, removed, imported)."""
        message = {
            "type": "mcp_event",
            "event": event,
            "server_name": server_name,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_workflow_event(
        self,
        event: str,
        definition_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast workflow/rule/agent definition event."""
        message = {
            "type": "workflow_event",
            "event": event,
            "definition_id": definition_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_project_event(
        self,
        event: str,
        project_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast project event (updated, deleted)."""
        message = {
            "type": "project_event",
            "event": event,
            "project_id": project_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_cron_event(
        self,
        event: str,
        job_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast cron job event (created, updated, deleted, run_triggered)."""
        message = {
            "type": "cron_event",
            "event": event,
            "job_id": job_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_agent_event(
        self,
        event: str,
        run_id: str,
        parent_session_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast agent event."""
        message = {
            "type": "agent_event",
            "event": event,
            "run_id": run_id,
            "parent_session_id": parent_session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_worktree_event(
        self,
        event: str,
        worktree_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast worktree event."""
        message = {
            "type": "worktree_event",
            "event": event,
            "worktree_id": worktree_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_autonomous_event(
        self,
        event: str,
        session_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast autonomous execution event."""
        message = {
            "type": "autonomous_event",
            "event": event,
            "session_id": session_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_task_event(
        self,
        event: str,
        task_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast task event (created, updated, closed, reopened)."""
        message = {
            "type": "task_event",
            "event": event,
            "task_id": task_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_pipeline_event(
        self,
        event: str,
        execution_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast pipeline execution event."""
        message = {
            "type": "pipeline_event",
            "event": event,
            "execution_id": execution_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_terminal_output(
        self,
        run_id: str,
        data: str,
    ) -> None:
        """Broadcast terminal output."""
        message = {
            "type": "terminal_output",
            "run_id": run_id,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self.broadcast(message)

    async def broadcast_tmux_session_event(
        self,
        event: str,
        session_name: str,
        socket: str,
    ) -> None:
        """Broadcast tmux session lifecycle event (created, killed).

        Bridges agent spawn/stop events to the tmux_session_event type
        that the Terminals page subscribes to for auto-refresh.
        """
        message = {
            "type": "tmux_session_event",
            "event": event,
            "session_name": session_name,
            "socket": socket,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self.broadcast(message)

    async def broadcast_agent_message(
        self,
        event: str,
        from_session: str,
        to_session: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast inter-agent message event (message_sent)."""
        message = {
            "type": "agent_message",
            "event": event,
            "from_session": from_session,
            "to_session": to_session,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_agent_command(
        self,
        event: str,
        from_session: str,
        to_session: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast inter-agent command event (command_sent, command_completed)."""
        message = {
            "type": "agent_command",
            "event": event,
            "from_session": from_session,
            "to_session": to_session,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

    async def broadcast_canvas_event(
        self,
        event: str,
        canvas_id: str,
        conversation_id: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast canvas interaction/update event."""
        message = {
            "type": "canvas_event",
            "event": event,
            "canvas_id": canvas_id,
            "conversation_id": conversation_id,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

"""WebSocket broadcast methods.

BroadcastMixin provides all broadcast_* methods for WebSocketServer.
Extracted from server.py as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed

from gobby.storage.attention import AttentionOrderingCoordinator
from gobby.utils.json_helpers import json_dumps

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore

logger = logging.getLogger(__name__)
BROADCAST_SEND_TIMEOUT_SECONDS = 2.0
BROADCAST_CLOSE_TIMEOUT_SECONDS = 1.0


class BroadcastMixin:
    """Mixin providing broadcast methods for WebSocketServer.

    Requires ``self.clients: dict[Any, dict[str, Any]]`` on the host class.
    """

    clients: dict[Any, dict[str, Any]]
    _attention_ordering: AttentionOrderingCoordinator | None = None
    _attention_metadata_store: AttentionMetadataStore | None = None

    def configure_attention_ordering(self, ordering: AttentionOrderingCoordinator) -> None:
        self._attention_ordering = ordering

    def configure_attention_metadata(self, store: AttentionMetadataStore) -> None:
        self._attention_metadata_store = store

    @property
    def attention_epoch(self) -> str | None:
        ordering = self._attention_ordering
        return ordering.epoch if ordering is not None else None

    @property
    def attention_seq(self) -> int:
        ordering = self._attention_ordering
        return ordering.seq if ordering is not None else 0

    @property
    def attention_ordering_lock(self) -> asyncio.Lock | None:
        ordering = self._attention_ordering
        return ordering.lock if ordering is not None else None

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
            "session_usage_updated",
            "token_event",
            "agent_event",
            "agent_message",
            "worktree_event",
            "autonomous_event",
            "pipeline_event",
            "terminal_output",
            "terminal_event",
            "skill_event",
            "mcp_event",
            "workflow_event",
            "project_event",
            "cron_event",
            "trace_event",
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

    async def _send_broadcast(self, websocket: Any, message: str) -> bool:
        """Send one broadcast without allowing a stalled client to block fan-out."""
        try:
            await asyncio.wait_for(
                websocket.send(message),
                timeout=BROADCAST_SEND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            self.clients.pop(websocket, None)
            logger.warning(
                "Broadcast send timed out after %.1fs; dropping client",
                BROADCAST_SEND_TIMEOUT_SECONDS,
            )
            try:
                await asyncio.wait_for(
                    websocket.close(code=1011, reason="Broadcast send timed out"),
                    timeout=BROADCAST_CLOSE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning("Timed-out broadcast client did not close promptly")
            except ConnectionClosed:
                pass
            except Exception as exc:
                logger.warning("Failed to close timed-out broadcast client: %s", exc)
            return False
        except ConnectionClosed:
            return False
        except Exception as exc:
            logger.warning("Broadcast failed for client: %s", exc)
            return False
        return True

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Broadcast message to all connected clients.

        Filters messages based on client subscriptions using _is_subscribed.

        Args:
            message: Dictionary to serialize and send
        """
        if not self.clients:
            return

        try:
            message_str = json_dumps(message)
        except (TypeError, ValueError) as exc:
            logger.warning("Broadcast payload is not JSON serializable: %s", exc)
            return
        recipients = []
        failed_count = 0
        for websocket in list(self.clients):
            try:
                if self._is_subscribed(websocket, message):
                    recipients.append(websocket)
            except Exception as exc:
                logger.warning("Broadcast subscription check failed for client: %s", exc)
                self.clients.pop(websocket, None)
                failed_count += 1

        results = await asyncio.gather(
            *(self._send_broadcast(websocket, message_str) for websocket in recipients)
        )
        sent_count = sum(results)
        failed_count += len(results) - sent_count
        for websocket, sent in zip(recipients, results, strict=True):
            if not sent:
                self.clients.pop(websocket, None)

        if sent_count > 0 or failed_count > 0:
            logger.debug(
                "Broadcast %s: %s sent, %s failed", message.get("type"), sent_count, failed_count
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

    async def broadcast_config_event(self, revision: int) -> None:
        """Broadcast one revision-only event for each newly reconciled epoch."""
        current = getattr(self, "_last_config_event_revision", -1)
        if revision <= current:
            return
        self._last_config_event_revision = revision
        await self.broadcast({"type": "config_event", "revision": revision})

    async def broadcast_session_usage_updated(self, payload: dict[str, Any]) -> None:
        """Broadcast session aggregate token usage refresh."""
        message = {
            **payload,
            "type": "session_usage_updated",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self.broadcast(message)

    async def broadcast_token_event(self, payload: dict[str, Any]) -> None:
        """Broadcast a transcript-derived token event."""
        message = {
            **payload,
            "type": "token_event",
            "timestamp": datetime.now(UTC).isoformat(),
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

    async def broadcast_trace_event(self, span: dict[str, Any]) -> None:
        """Broadcast an OpenTelemetry span as a trace event."""
        message = {
            "type": "trace_event",
            "span": span,
            "trace_id": span.get("trace_id"),
            "timestamp": datetime.now(UTC).isoformat(),
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
        entry_id = kwargs.get("entry_id")
        metadata_store = self._attention_metadata_store
        if metadata_store is not None and isinstance(entry_id, str) and "metadata" not in kwargs:
            metadata = metadata_store.get(entry_id)
            if metadata is not None:
                kwargs["metadata"] = metadata
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
        terminal_id: str,
        data: str,
        attachment_id: str | None = None,
    ) -> None:
        """Broadcast terminal output keyed by durable terminal id."""
        message = {
            "type": "terminal_output",
            "terminal_id": terminal_id,
            "attachment_id": attachment_id,
            "data": data,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self.broadcast(message)

    async def broadcast_tmux_session_event(
        self,
        event: str,
        terminal_id: str = "",
        session_name: str | None = None,
        socket: str | None = None,
    ) -> None:
        """Broadcast terminal lifecycle events to subscribed clients."""
        del session_name, socket
        message = {
            "type": "terminal_event",
            "event": event,
            "terminal_id": terminal_id,
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

    async def broadcast_communications_event(
        self,
        event: str,
        **kwargs: Any,
    ) -> None:
        """Broadcast communications event."""
        message = {
            "type": "communications_event",
            "event": event,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        await self.broadcast(message)

"""Inter-agent messaging and command tools for the gobby-agents MCP server.

Provides P2P messaging and command coordination between sessions:
- send_message: target-based messaging with same-project validation for sessions
- deliver_pending_messages: Fetch and mark undelivered messages
- get_inter_session_messages: Read-only query of message history
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.storage.inter_session_messages import normalize_message_direction

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.sessions.mailbox import WakeDispatcherProtocol
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager

# Type alias for the broadcast callback
BroadcastFn = Callable[..., Coroutine[Any, Any, None]]

logger = logging.getLogger(__name__)


def _message_metadata(msg: Any) -> dict[str, Any]:
    """Parse optional JSON metadata from an inter-session message."""
    raw = getattr(msg, "metadata_json", None)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _message_delivery_payload(msg: Any) -> dict[str, Any]:
    """Return pending-message payload with delivery context included."""
    raw_payload = msg.to_brief()
    payload: dict[str, Any] = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
    metadata = _message_metadata(msg)
    if metadata:
        payload["metadata"] = metadata
    for key in ("run_id", "task_id", "completion_id", "signoff_message"):
        value = metadata.get(key)
        if value:
            payload[key] = value
    payload["has_signoff"] = bool(metadata.get("signoff_message") or metadata.get("signoff"))
    return payload


def add_messaging_tools(
    registry: InternalToolRegistry,
    message_manager: InterSessionMessageManager,
    session_manager: SessionManager,
    db: HubDatabase,
    broadcast_fn: BroadcastFn | None = None,
    wake_dispatcher: WakeDispatcherProtocol | None = None,
) -> None:
    """Add inter-agent messaging and command tools to a registry.

    Args:
        registry: The InternalToolRegistry to add tools to
        message_manager: For persisting inter-session messages
        session_manager: For resolving session relationships
        db: Database for direct queries (agent_runs)
        broadcast_fn: Optional async callback for WebSocket broadcasts.
            Called with (msg_type=, event=, **kwargs) on successful operations.
    """

    def _resolve(ref: str) -> str:
        """Resolve session reference to UUID."""
        from gobby.utils.project_context import get_project_context

        ctx = get_project_context()
        project_id = ctx.get("id") if ctx else None
        return session_manager.resolve_session_reference(ref, project_id)

    from gobby.sessions.mailbox import MailboxService

    mailbox = MailboxService(
        db=db,
        message_manager=message_manager,
        session_manager=session_manager,
        wake_dispatcher=wake_dispatcher,
    )

    # ── send_message ───────────────────────────────────────────────

    @registry.tool(
        name="send_message",
        description=(
            "Send a message to an explicit target selector: session, agent, "
            "project, build, or all. Session targets validate same-project "
            "delivery. Messages are automatically injected "
            "into the recipient's context on their next tool call via hook "
            "rules — no polling or mailbox fetch needed. Also auto-writes "
            "to agent_runs.result when sending to parent. Pass target_id for "
            "target='session' (session ref), target='agent' (agent run id), "
            "target='project' (project id/name), and target='build' (build run id, "
            "build input ref, or root task ref). target='all' forbids target_id. "
            "Optional fields such as priority, message_type, metadata, and "
            "include_wakeup are keyword-only."
        ),
    )
    async def send_message(
        from_session: str,
        target: str,
        content: str,
        target_id: str | None = None,
        *,
        priority: str = "normal",
        include_wakeup: bool = False,
        message_type: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            content = content.strip()
            if not content:
                return {"success": False, "error": "content is required."}
            normalized_target = target.strip().lower()
            if normalized_target == "all" and target_id is not None:
                return {
                    "success": False,
                    "error": "target_id is not allowed when target='all'.",
                }
            if normalized_target == "session":
                if target_id is None or not target_id.strip():
                    return {
                        "success": False,
                        "error": "target_id is required when target='session'.",
                    }

            from_id = _resolve(from_session)

            resolved_target_id = target_id
            if normalized_target == "session":
                assert target_id is not None
                resolved_target_id = _resolve(target_id)

            send_result = await mailbox.send(
                from_session_id=from_id,
                target=normalized_target,
                target_id=resolved_target_id,
                include_wakeup=include_wakeup,
                content=content,
                priority=priority,
                message_type=message_type,
                metadata=metadata,
            )
            msg = send_result.messages[0] if len(send_result.messages) == 1 else None

            from_sess = session_manager.get(from_id)

            # Auto-write to agent_runs.result when sending to parent
            if (
                from_sess
                and len(send_result.recipient_session_ids) == 1
                and from_sess.parent_session_id == send_result.recipient_session_ids[0]
            ):
                try:
                    row = db.fetchone(
                        "SELECT id FROM agent_runs WHERE child_session_id = ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (from_id,),
                    )
                    if row:
                        now = datetime.now(UTC).isoformat()
                        db.execute(
                            "UPDATE agent_runs SET result = ?, updated_at = ? WHERE id = ?",
                            (content, now, row["id"]),
                        )
                except Exception as e:
                    logger.warning(f"Failed to write to agent_runs.result: {e}")

            # Broadcast agent_message event
            failed_broadcasts: list[dict[str, Any]] = []
            if broadcast_fn:
                for recipient_id in send_result.recipient_session_ids:
                    try:
                        await broadcast_fn(
                            msg_type="agent_message",
                            event="message_sent",
                            from_session=from_id,
                            to_session=recipient_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to broadcast agent_message",
                            extra={
                                "from_session": from_id,
                                "to_session": recipient_id,
                                "broadcast_id": send_result.broadcast_id,
                            },
                            exc_info=True,
                        )
                        failed_broadcasts.append(
                            {
                                "recipient_session_id": recipient_id,
                                "error": str(e),
                            }
                        )
            payload = send_result.to_dict()
            payload["failed_broadcasts"] = failed_broadcasts
            payload["success"] = bool(payload.get("success")) and not failed_broadcasts
            payload["message"] = msg.to_dict() if msg is not None else None
            return payload

        except Exception as e:
            logger.error(f"send_message failed: {e}")
            return {"success": False, "error": str(e)}

    # ── deliver_pending_messages ───────────────────────────────────

    @registry.tool(
        name="deliver_pending_messages",
        description=(
            "Fetch undelivered messages for a session and mark them as delivered. "
            "Use this to inject pending messages as context."
        ),
    )
    async def deliver_pending_messages(
        target_session_id: str,
    ) -> dict[str, Any]:
        try:
            resolved_id = _resolve(target_session_id)
            undelivered = message_manager.get_undelivered_messages(resolved_id)

            messages = []
            for msg in undelivered:
                message_manager.mark_delivered(msg.id)
                messages.append(_message_delivery_payload(msg))

            return {
                "success": True,
                "messages": messages,
                "count": len(messages),
            }

        except Exception as e:
            logger.error(f"deliver_pending_messages failed: {e}")
            return {"success": False, "error": str(e)}

    # ── get_inter_session_messages ────────────────────────────────

    @registry.tool(
        name="get_inter_session_messages",
        description=(
            "Read-only query of inter-session message history. "
            "Returns sent and/or received messages without marking them "
            "as delivered or read. Use for debugging, audit, and visibility."
        ),
    )
    async def get_inter_session_messages(
        target_session_id: str,
        direction: str = "all",
        unread_only: bool = False,
        undelivered_only: bool = False,
        message_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            try:
                normalized_direction = normalize_message_direction(direction)
            except ValueError as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "error_code": "invalid_direction",
                }
            resolved_id = _resolve(target_session_id)
            messages = message_manager.list_messages(
                session_id=resolved_id,
                direction=normalized_direction,
                unread_only=unread_only,
                undelivered_only=undelivered_only,
                message_type=message_type,
                limit=limit,
                offset=offset,
            )

            return {
                "success": True,
                "messages": [m.to_brief() for m in messages],
                "count": len(messages),
            }

        except Exception as e:
            logger.error(f"get_inter_session_messages failed: {e}")
            return {"success": False, "error": str(e)}

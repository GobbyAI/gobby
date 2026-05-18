"""Durable inter-session mailbox delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.sessions import SYSTEM_SESSION_ID

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.inter_session_messages import (
        InterSessionMessage,
        InterSessionMessageManager,
    )
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager


ACTIVE_AGENT_RUN_STATUSES = ("pending", "running")
DELIVERABLE_SESSION_STATUSES = ("active", "paused")

logger = logging.getLogger(__name__)


class WakeDispatcherProtocol(Protocol):
    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]: ...


@dataclass
class MailboxSendResult:
    """Result for direct or fanout mailbox delivery."""

    messages: list[InterSessionMessage] = field(default_factory=list)
    recipient_session_ids: list[str] = field(default_factory=list)
    broadcast_id: str | None = None
    wake_results: list[dict[str, Any]] = field(default_factory=list)
    failed_broadcasts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def message_ids(self) -> list[str]:
        return [message.id for message in self.messages]

    @property
    def success(self) -> bool:
        return not self.failed_broadcasts

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "message_ids": self.message_ids,
            "recipient_session_ids": self.recipient_session_ids,
            "broadcast_id": self.broadcast_id,
            "wake_results": self.wake_results,
            "failed_broadcasts": self.failed_broadcasts,
        }


class MailboxService:
    """Stores durable mailbox messages and optionally wakes recipients."""

    def __init__(
        self,
        *,
        db: DatabaseProtocol,
        message_manager: InterSessionMessageManager,
        session_manager: SessionManager,
        wake_dispatcher: WakeDispatcherProtocol | None = None,
    ) -> None:
        self._db = db
        self._message_manager = message_manager
        self._session_manager = session_manager
        self._wake_dispatcher = wake_dispatcher

    async def send(
        self,
        *,
        from_session_id: str,
        content: str,
        to_session_id: str | None = None,
        send_to_all: bool = False,
        include_wakeup: bool = False,
        priority: str = "normal",
        message_type: str = "message",
        metadata: Mapping[str, Any] | None = None,
        project_id: str | None = None,
    ) -> MailboxSendResult:
        """Send a mailbox message directly or to all active project agents."""
        content = content.strip()
        if not content:
            raise ValueError("content is required")
        if send_to_all and to_session_id:
            raise ValueError("to_session_id cannot be combined with send_to_all=true")
        if not send_to_all and not to_session_id:
            raise ValueError("to_session_id is required when send_to_all=false")

        resolved_project_id: str | None
        if send_to_all:
            resolved_project_id = self._resolve_project_id(from_session_id, project_id)
            recipient_ids = self._broadcast_recipient_session_ids(
                from_session_id=from_session_id,
                project_id=resolved_project_id,
            )
            broadcast_id = str(uuid.uuid4())
            if not recipient_ids:
                logger.info(
                    "Mailbox broadcast resolved no recipients",
                    extra={
                        "from_session_id": from_session_id,
                        "resolved_project_id": resolved_project_id,
                        "broadcast_id": broadcast_id,
                    },
                )
                return MailboxSendResult(
                    recipient_session_ids=[],
                    broadcast_id=broadcast_id,
                )
        else:
            resolved_project_id = project_id
            assert to_session_id is not None
            recipient_ids = [
                self._validate_direct_recipient(
                    from_session_id=from_session_id,
                    to_session_id=to_session_id,
                    project_id=project_id,
                )
            ]
            broadcast_id = None

        messages = []
        for recipient_id in recipient_ids:
            metadata_json = self._metadata_json(
                metadata=metadata,
                broadcast_id=broadcast_id,
                project_id=resolved_project_id,
                from_session_id=from_session_id,
            )
            messages.append(
                self._message_manager.create_message(
                    from_session=from_session_id,
                    to_session=recipient_id,
                    content=content,
                    priority=priority,
                    message_type=message_type,
                    metadata_json=metadata_json,
                )
            )

        wake_results: list[dict[str, Any]] = []
        if include_wakeup:
            wake_results = [
                self._normalize_wake_result(recipient_id, result)
                for recipient_id, result in zip(
                    recipient_ids,
                    await asyncio.gather(
                        *(self._wake(rid) for rid in recipient_ids),
                        return_exceptions=True,
                    ),
                    strict=True,
                )
            ]

        return MailboxSendResult(
            messages=messages,
            recipient_session_ids=recipient_ids,
            broadcast_id=broadcast_id,
            wake_results=wake_results,
        )

    @staticmethod
    def _normalize_wake_result(session_id: str, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, BaseException):
            detail = str(result) or type(result).__name__
            return {
                "session_id": session_id,
                "delivered": False,
                "method": None,
                "error": detail,
                "error_code": "wake_dispatch_failed",
                "error_message": detail,
            }
        return {"session_id": session_id, "delivered": False, "method": None}

    def _resolve_project_id(self, from_session_id: str, project_id: str | None) -> str:
        if project_id:
            return project_id
        sender: Session | None = self._session_manager.get(from_session_id)
        if sender is None:
            raise ValueError(f"Sender session not found: {from_session_id}")
        if from_session_id == SYSTEM_SESSION_ID:
            raise ValueError("project_id is required for system broadcast messages")
        return str(sender.project_id)

    def _validate_direct_recipient(
        self,
        *,
        from_session_id: str,
        to_session_id: str,
        project_id: str | None,
    ) -> str:
        sender: Session | None = self._session_manager.get(from_session_id)
        if sender is None:
            raise ValueError(f"Sender session not found: {from_session_id}")

        recipient: Session | None = self._session_manager.get(to_session_id)
        if recipient is None:
            raise ValueError(f"Recipient session not found: {to_session_id}")

        if project_id and recipient.project_id != project_id:
            raise ValueError("Recipient session is outside the target project")
        # System-originated messages are internal daemon notifications scoped by
        # explicit project_id, so they bypass sender/recipient project equality.
        if from_session_id != SYSTEM_SESSION_ID and sender.project_id != recipient.project_id:
            raise ValueError(
                "Cross-project messaging not allowed. "
                f"Sender project: {sender.project_id}, recipient project: {recipient.project_id}"
            )
        return to_session_id

    def _broadcast_recipient_session_ids(
        self,
        *,
        from_session_id: str,
        project_id: str,
    ) -> list[str]:
        active_status_placeholders = ",".join("?" for _ in ACTIVE_AGENT_RUN_STATUSES)
        rows = self._db.fetchall(
            f"""
            SELECT
                ar.child_session_id,
                ar.parent_session_id,
                child.status AS child_status,
                parent.status AS parent_status
            FROM agent_runs ar
            LEFT JOIN sessions child ON child.id = ar.child_session_id
            LEFT JOIN sessions parent ON parent.id = ar.parent_session_id
            WHERE ar.status IN ({active_status_placeholders})
              AND COALESCE(child.project_id, parent.project_id) = ?
            ORDER BY ar.created_at ASC
            """,
            (*ACTIVE_AGENT_RUN_STATUSES, project_id),
        )

        recipients: list[str] = []
        seen: set[str] = set()
        for row in rows:
            child_id = row["child_session_id"]
            child_status = row["child_status"]
            parent_id = row["parent_session_id"]
            parent_status = row["parent_status"]
            recipient_id = self._select_agent_recipient(
                child_id=child_id,
                child_status=child_status,
                parent_id=parent_id,
                parent_status=parent_status,
            )
            if recipient_id is None or recipient_id == from_session_id or recipient_id in seen:
                continue
            recipients.append(recipient_id)
            seen.add(recipient_id)
        return recipients

    @staticmethod
    def _select_agent_recipient(
        *,
        child_id: str | None,
        child_status: str | None,
        parent_id: str | None,
        parent_status: str | None,
    ) -> str | None:
        if child_id and child_status in DELIVERABLE_SESSION_STATUSES:
            return child_id
        if parent_id and parent_status in DELIVERABLE_SESSION_STATUSES:
            return parent_id
        return None

    @staticmethod
    def _metadata_json(
        *,
        metadata: Mapping[str, Any] | None,
        broadcast_id: str | None,
        project_id: str | None,
        from_session_id: str,
    ) -> str | None:
        if metadata is None and broadcast_id is None:
            return None

        payload = dict(metadata or {})
        if broadcast_id is not None:
            payload["broadcast_id"] = broadcast_id
            payload["broadcast"] = {
                "send_to_all": True,
                "selector": {
                    "project_id": project_id,
                    "agent_run_status": list(ACTIVE_AGENT_RUN_STATUSES),
                    "session_status": list(DELIVERABLE_SESSION_STATUSES),
                    "exclude_session_id": from_session_id,
                },
            }
        return json.dumps(payload, default=str, sort_keys=True)

    async def _wake(self, session_id: str) -> dict[str, Any]:
        if self._wake_dispatcher is None:
            return {
                "session_id": session_id,
                "delivered": False,
                "method": None,
                "error": "wake_dispatcher_unavailable",
                "error_code": "wake_dispatcher_unavailable",
                "error_message": "Wake dispatcher is unavailable",
            }
        try:
            result = await self._wake_dispatcher.dispatch_live_wake(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Mailbox wake dispatch failed for session %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            return {
                "session_id": session_id,
                "delivered": False,
                "method": None,
                "error": str(exc),
                "error_code": "wake_dispatch_failed",
                "error_message": str(exc),
            }
        if isinstance(result, dict):
            return result
        return {"session_id": session_id, "delivered": False, "method": None}

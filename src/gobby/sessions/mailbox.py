"""Durable inter-session mailbox delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from gobby.agents.team_communications import (
    RECIPIENT_REFUSED,
    REFUSAL,
    recipient_policy,
    relaunched_seat,
)
from gobby.sessions.clear_continuation import resolve_clear_successor
from gobby.sessions.mailbox_delivery import WakeDispatcherProtocol, dispatch_mailbox_wakes
from gobby.sessions.mailbox_targets import resolve_broadcast_selection
from gobby.storage.sessions import (
    LIVE_SESSION_STATUS_ORDER,
    ensure_system_session,
    system_session_id,
)
from gobby.utils.uuid_validation import parse_uuid_reference

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import (
    InterSessionMessage,
    InterSessionMessageManager,
)
from gobby.storage.sessions import TERMINAL_SESSION_STATUSES

if TYPE_CHECKING:
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager


ACTIVE_AGENT_RUN_STATUSES = ("queued", "pending", "running")
DELIVERABLE_SESSION_STATUSES = LIVE_SESSION_STATUS_ORDER
MESSAGE_TARGETS = ("global", "project", "parent", "session", "agent")

logger = logging.getLogger(__name__)


@dataclass
class MailboxSendResult:
    """Result for direct or fanout mailbox delivery."""

    messages: list[InterSessionMessage] = field(default_factory=list)
    recipient_session_ids: list[str] = field(default_factory=list)
    broadcast_id: str | None = None
    target: str | None = None
    target_id: str | None = None
    selector_metadata: dict[str, Any] | None = None
    wake_results: list[dict[str, Any]] = field(default_factory=list)
    failed_broadcasts: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error: str | None = None

    @property
    def message_ids(self) -> list[str]:
        return [message.id for message in self.messages]

    @property
    def success(self) -> bool:
        """Report durable mailbox persistence, independent of live side effects."""
        return self.error_code is None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "success": self.success,
            "message_ids": self.message_ids,
            "recipient_session_ids": self.recipient_session_ids,
            "broadcast_id": self.broadcast_id,
            "target": self.target,
            "target_id": self.target_id,
            "selector_metadata": self.selector_metadata,
            "wake_results": self.wake_results,
            "failed_broadcasts": self.failed_broadcasts,
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.error is not None:
            result["error"] = self.error
        return result


@dataclass(frozen=True)
class MailboxTargetResolution:
    """Resolved mailbox target recipients and traceable selector metadata."""

    target: str
    target_id: str | None
    recipient_session_ids: list[str]
    selector_metadata: dict[str, Any]
    fanout: bool = False


class MailboxService:
    """Stores durable mailbox messages and optionally wakes recipients."""

    def __init__(
        self,
        *,
        db: HubDatabase,
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
        target: str,
        content: str,
        target_id: str | None = None,
        message_id: str | None = None,
        wake: bool | None = False,
        priority: str = "normal",
        message_type: str = "message",
        metadata: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        preserve_content: bool = False,
        replace_composer: bool = False,
    ) -> MailboxSendResult:
        """Send a mailbox message to an explicit resolved target selector.

        ``wake=None`` takes the target default: direct targets wake and fanout stays
        queued. Recipient definitions declare required sender capabilities; direct
        targets refuse unauthorized senders and fanout withholds those recipients.
        """
        if not preserve_content:
            content = content.strip()
        if not content.strip():
            raise ValueError("content is required")
        if replace_composer and (
            from_session_id != system_session_id() or target != "session" or not wake
        ):
            raise ValueError(
                "Composer replacement is reserved for direct operator communications wakes"
            )

        messages = []
        with self._db.transaction():
            resolution = self.resolve_target(
                from_session_id=from_session_id,
                target=target,
                target_id=target_id,
                project_id=project_id,
            )
            recipient_ids = resolution.recipient_session_ids
            policy = recipient_policy(self._db, from_session_id, recipient_ids)
            if policy.withheld and not resolution.fanout:
                return MailboxSendResult(
                    target=resolution.target,
                    target_id=resolution.target_id,
                    selector_metadata=resolution.selector_metadata,
                    error_code=RECIPIENT_REFUSED,
                    error=REFUSAL,
                )
            if policy.withheld:
                recipient_ids = [r for r in recipient_ids if r not in policy.withheld]
                resolution = replace(
                    resolution,
                    recipient_session_ids=recipient_ids,
                    selector_metadata={
                        **resolution.selector_metadata,
                        "withheld_recipients": len(policy.withheld),
                    },
                )
            if wake is None:
                wake = not resolution.fanout
            if message_id is not None and (resolution.fanout or len(recipient_ids) != 1):
                raise ValueError("message_id requires exactly one direct recipient")
            broadcast_id = str(uuid.uuid4()) if resolution.fanout else None
            if resolution.fanout and not recipient_ids:
                logger.info(
                    "Mailbox target resolved no recipients",
                    extra={
                        "from_session_id": from_session_id,
                        "target": resolution.target,
                        "target_id": resolution.target_id,
                        "broadcast_id": broadcast_id,
                    },
                )
                return MailboxSendResult(
                    recipient_session_ids=[],
                    broadcast_id=broadcast_id,
                    target=resolution.target,
                    target_id=resolution.target_id,
                    selector_metadata=resolution.selector_metadata,
                    error_code="no_recipients",
                    error="No recipients matched the target selector.",
                )

            if recipient_ids and from_session_id == system_session_id():
                ensure_system_session(self._db)

            for recipient_id in recipient_ids:
                message_metadata = dict(metadata or {})
                # This capability is an internal ingress argument, never caller metadata.
                message_metadata.pop("operator_comms", None)
                if replace_composer:
                    message_metadata["operator_comms"] = True
                if (
                    resolution.target == "session"
                    and resolution.target_id is not None
                    and resolution.target_id != recipient_id
                ):
                    message_metadata["redirected_from"] = resolution.target_id
                metadata_json = self._metadata_json(
                    metadata=message_metadata,
                    broadcast_id=broadcast_id,
                    resolution=resolution,
                    wake_requested=wake,
                )
                messages.append(
                    self._message_manager.create_message(
                        from_session=from_session_id,
                        to_session=recipient_id,
                        content=content,
                        priority=priority,
                        message_type=message_type,
                        metadata_json=metadata_json,
                        message_id=message_id,
                    )
                )

        wake_results: list[dict[str, Any]] = []
        if wake:
            # Persistence is complete. The dispatcher owns live-wake policy and
            # channel-specific bounds; an outer timeout can interrupt tmux submission.
            wake_results = await dispatch_mailbox_wakes(
                self._wake_dispatcher,
                self._session_manager,
                messages,
                recipient_ids,
                priority=priority,
            )

        return MailboxSendResult(
            messages=messages,
            recipient_session_ids=recipient_ids,
            broadcast_id=broadcast_id,
            target=resolution.target,
            target_id=resolution.target_id,
            selector_metadata=resolution.selector_metadata,
            wake_results=wake_results,
        )

    async def wake_committed(self, message_id: str) -> list[dict[str, Any]]:
        """Wake the recipient of a mailbox row another transaction committed, if it exists."""
        message = await asyncio.to_thread(self._message_manager.get_message, message_id)
        if message is None:
            return []
        return await dispatch_mailbox_wakes(
            self._wake_dispatcher,
            self._session_manager,
            [message],
            [message.to_session],
            priority=message.priority,
        )

    def resolve_target(
        self,
        *,
        from_session_id: str,
        target: str,
        target_id: str | None,
        project_id: str | None = None,
    ) -> MailboxTargetResolution:
        """Resolve a message target into ordered, deduplicated recipient sessions."""
        normalized_target = target.strip().lower()
        if normalized_target not in MESSAGE_TARGETS:
            expected = ", ".join(MESSAGE_TARGETS)
            raise ValueError(f"Unknown message target '{target}'. Expected one of: {expected}")

        clean_target_id = target_id.strip() if isinstance(target_id, str) else target_id
        if clean_target_id == "":
            clean_target_id = None

        if normalized_target in {"global", "project"}:
            if clean_target_id is not None:
                raise ValueError(f"target_id is not allowed when target='{normalized_target}'")
            selection = resolve_broadcast_selection(
                db=self._db,
                session_store=self._session_manager,
                target=normalized_target,
                from_session_id=from_session_id,
                project_id=project_id,
                resolve_project_ref=self._resolve_project_ref,
            )
            return MailboxTargetResolution(
                target=normalized_target,
                target_id=None,
                recipient_session_ids=selection.recipient_session_ids,
                selector_metadata=selection.selector_metadata,
                fanout=True,
            )

        if normalized_target == "parent":
            if target_id is not None:
                raise ValueError("target_id is not allowed when target='parent'")
            sender = self._session_manager.get(from_session_id)
            if sender is None:
                raise ValueError("Sender session not found")
            # parent_session_id is lineage: an interactive session's parent is the
            # clear predecessor it continues, which can resolve back to the sender.
            if not (sender.agent_run_id or sender.agent_depth):
                raise ValueError("target='parent' is only available to spawned agent sessions")
            if not sender.parent_session_id:
                raise ValueError("Sender session has no parent")
            recipient_id = self._validate_direct_recipient(
                from_session_id=from_session_id,
                to_session_id=sender.parent_session_id,
                project_id=project_id,
                allow_cross_project=True,
            )
            return MailboxTargetResolution(
                target="parent",
                target_id=None,
                recipient_session_ids=[recipient_id],
                selector_metadata={"target": "parent", "session_id": recipient_id},
            )

        if clean_target_id is None:
            raise ValueError(f"target_id is required when target='{normalized_target}'")

        if normalized_target == "session":
            # Direct session targets are addressed unambiguously (full UUID or
            # '<project>#N'), so delivery crosses project boundaries.
            recipient_id = self._validate_direct_recipient(
                from_session_id=from_session_id,
                to_session_id=clean_target_id,
                project_id=project_id,
                allow_cross_project=True,
            )
            return MailboxTargetResolution(
                target=normalized_target,
                target_id=clean_target_id,
                recipient_session_ids=[recipient_id],
                selector_metadata={"target": "session", "session_id": recipient_id},
            )

        return self._resolve_agent_target(
            from_session_id=from_session_id,
            agent_run_id=clean_target_id,
            project_id=project_id,
        )

    def _resolve_project_id(self, from_session_id: str, project_id: str | None) -> str:
        if project_id is not None:
            return self._resolve_project_ref(project_id)
        if from_session_id == system_session_id():
            raise ValueError("project_id is required for system broadcast messages")
        sender: Session | None = self._session_manager.get(from_session_id)
        if sender is None:
            raise ValueError(f"Sender session not found: {from_session_id}")
        return str(sender.project_id)

    def _resolve_project_ref(self, project_ref: str) -> str:
        row = None
        if parse_uuid_reference(project_ref) is not None:
            row = self._db.fetchone(
                "SELECT id FROM projects WHERE id = %s AND deleted_at IS NULL",
                (project_ref,),
            )
        if row is None:
            row = self._db.fetchone(
                "SELECT id FROM projects WHERE name = %s AND deleted_at IS NULL",
                (project_ref,),
            )
        if row is None:
            raise ValueError(f"Project target not found: {project_ref}")
        return str(row["id"])

    def _validate_direct_recipient(
        self,
        *,
        from_session_id: str,
        to_session_id: str,
        project_id: str | None,
        allow_cross_project: bool = False,
    ) -> str:
        recipient: Session | None = self._session_manager.get(to_session_id)
        if recipient is None:
            raise ValueError(f"Recipient session not found: {to_session_id}")
        if recipient.status in TERMINAL_SESSION_STATUSES:
            successor_id = resolve_clear_successor(self._db, to_session_id)
            if successor_id is not None:
                successor = self._session_manager.get(successor_id)
                if successor is None:
                    raise ValueError(f"Recipient session not found: {successor_id}")
                if successor.status in TERMINAL_SESSION_STATUSES:
                    raise ValueError(f"Recipient clear successor is not live: {successor_id}")
                recipient = successor
                to_session_id = successor.id
        if recipient.status in TERMINAL_SESSION_STATUSES:
            holder = relaunched_seat(self._db, to_session_id, recipient.project_id)
            relaunched = self._session_manager.get(holder) if holder else None
            if relaunched is not None:
                recipient = relaunched
                to_session_id = relaunched.id

        if project_id and recipient.project_id != self._resolve_project_ref(project_id):
            raise ValueError("Recipient session is outside the target project")
        # System-originated messages are internal daemon notifications scoped by
        # explicit project_id, so they bypass sender/recipient project equality.
        if from_session_id != system_session_id() and not allow_cross_project:
            sender: Session | None = self._session_manager.get(from_session_id)
            if sender is None:
                raise ValueError(f"Sender session not found: {from_session_id}")
            if sender.project_id == recipient.project_id:
                return to_session_id
            raise ValueError(
                "Cross-project messaging not allowed. "
                f"Sender project: {sender.project_id}, recipient project: {recipient.project_id}"
            )
        return to_session_id

    def _resolve_agent_target(
        self,
        *,
        from_session_id: str,
        agent_run_id: str,
        project_id: str | None,
    ) -> MailboxTargetResolution:
        active_status_placeholders = ",".join("%s" for _ in ACTIVE_AGENT_RUN_STATUSES)
        row = self._db.fetchone(
            f"""
            SELECT
                ar.id,
                ar.status,
                ar.task_id,
                ar.child_session_id,
                ar.parent_session_id,
                child.status AS child_status,
                parent.status AS parent_status
            FROM agent_runs ar
            LEFT JOIN sessions child ON child.id = ar.child_session_id
            LEFT JOIN sessions parent ON parent.id = ar.parent_session_id
            WHERE ar.id = %s
              AND ar.status IN ({active_status_placeholders})
            """,
            (agent_run_id, *ACTIVE_AGENT_RUN_STATUSES),
        )
        if row is None:
            raise ValueError(f"Agent target not found or inactive: {agent_run_id}")

        recipient_id = self._select_agent_recipient(
            child_id=row["child_session_id"],
            child_status=row["child_status"],
            parent_id=row["parent_session_id"],
            parent_status=row["parent_status"],
        )
        if recipient_id is None:
            raise ValueError(f"Agent target has no deliverable session: {agent_run_id}")

        task_id = str(row["task_id"]) if row["task_id"] is not None else None
        recipient_id = self._validate_direct_recipient(
            from_session_id=from_session_id,
            to_session_id=recipient_id,
            project_id=project_id,
            allow_cross_project=False,
        )
        return MailboxTargetResolution(
            target="agent",
            target_id=agent_run_id,
            recipient_session_ids=[recipient_id],
            selector_metadata={
                "target": "agent",
                "agent_run_id": agent_run_id,
                "agent_run_status": row["status"],
                "task_id": task_id,
            },
        )

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
        resolution: MailboxTargetResolution,
        wake_requested: bool,
    ) -> str:
        payload = dict(metadata or {})
        payload["wake_requested"] = wake_requested
        if broadcast_id is not None:
            payload["broadcast_id"] = broadcast_id
            payload["broadcast"] = {
                "target": resolution.target,
                "target_id": resolution.target_id,
                "selector": resolution.selector_metadata,
            }
        return json.dumps(payload, default=str, sort_keys=True)

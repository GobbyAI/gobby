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
from gobby.storage.tasks._id import resolve_task_reference
from gobby.storage.tasks._models import TaskNotFoundError

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.inter_session_messages import (
        InterSessionMessage,
        InterSessionMessageManager,
    )
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager


ACTIVE_AGENT_RUN_STATUSES = ("pending", "running")
DELIVERABLE_SESSION_STATUSES = ("active", "paused")
MESSAGE_TARGETS = ("session", "agent", "project", "build", "all")

logger = logging.getLogger(__name__)


class WakeDispatcherProtocol(Protocol):
    async def dispatch_live_wake(self, session_id: str) -> dict[str, Any]: ...


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
            "target": self.target,
            "target_id": self.target_id,
            "selector_metadata": self.selector_metadata,
            "wake_results": self.wake_results,
            "failed_broadcasts": self.failed_broadcasts,
        }


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
        include_wakeup: bool = False,
        priority: str = "normal",
        message_type: str = "message",
        metadata: Mapping[str, Any] | None = None,
        project_id: str | None = None,
    ) -> MailboxSendResult:
        """Send a mailbox message to an explicit resolved target selector."""
        content = content.strip()
        if not content:
            raise ValueError("content is required")

        resolution = self.resolve_target(
            from_session_id=from_session_id,
            target=target,
            target_id=target_id,
            project_id=project_id,
        )
        recipient_ids = resolution.recipient_session_ids
        if resolution.fanout:
            broadcast_id = str(uuid.uuid4())
            if not recipient_ids:
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
                )
        else:
            broadcast_id = None

        messages = []
        for recipient_id in recipient_ids:
            metadata_json = self._metadata_json(
                metadata=metadata,
                broadcast_id=broadcast_id,
                resolution=resolution,
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
            target=resolution.target,
            target_id=resolution.target_id,
            selector_metadata=resolution.selector_metadata,
            wake_results=wake_results,
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

        if normalized_target == "all":
            if clean_target_id is not None:
                raise ValueError("target_id is not allowed when target='all'")
            return MailboxTargetResolution(
                target=normalized_target,
                target_id=None,
                recipient_session_ids=self._all_recipient_session_ids(from_session_id),
                selector_metadata={
                    "target": "all",
                    "session_status": list(DELIVERABLE_SESSION_STATUSES),
                    "exclude_session_id": from_session_id,
                    "exclude_system_session": True,
                },
                fanout=True,
            )

        if clean_target_id is None:
            raise ValueError(f"target_id is required when target='{normalized_target}'")

        if normalized_target == "session":
            recipient_id = self._validate_direct_recipient(
                from_session_id=from_session_id,
                to_session_id=clean_target_id,
                project_id=project_id,
            )
            return MailboxTargetResolution(
                target=normalized_target,
                target_id=clean_target_id,
                recipient_session_ids=[recipient_id],
                selector_metadata={"target": "session", "session_id": recipient_id},
            )

        if normalized_target == "agent":
            return self._resolve_agent_target(
                from_session_id=from_session_id,
                agent_run_id=clean_target_id,
                project_id=project_id,
            )

        if normalized_target == "project":
            resolved_project_id = self._resolve_project_ref(clean_target_id)
            return MailboxTargetResolution(
                target=normalized_target,
                target_id=resolved_project_id,
                recipient_session_ids=self._agent_recipient_session_ids(
                    from_session_id=from_session_id,
                    project_id=resolved_project_id,
                ),
                selector_metadata=self._agent_selector_metadata(
                    target="project",
                    project_id=resolved_project_id,
                    exclude_session_id=from_session_id,
                ),
                fanout=True,
            )

        root_task_id, build_selector = self._resolve_build_target(
            target_id=clean_target_id,
            from_session_id=from_session_id,
            project_id=project_id,
        )
        return MailboxTargetResolution(
            target=normalized_target,
            target_id=clean_target_id,
            recipient_session_ids=self._build_recipient_session_ids(
                from_session_id=from_session_id,
                root_task_id=root_task_id,
            ),
            selector_metadata={
                **build_selector,
                **self._agent_selector_metadata(
                    target="build",
                    project_id=build_selector.get("project_id"),
                    exclude_session_id=from_session_id,
                ),
                "root_task_id": root_task_id,
            },
            fanout=True,
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

    def _resolve_project_ref(self, project_ref: str) -> str:
        row = self._db.fetchone(
            "SELECT id FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_ref,),
        )
        if row is None:
            row = self._db.fetchone(
                "SELECT id FROM projects WHERE name = ? AND deleted_at IS NULL",
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

    def _all_recipient_session_ids(self, from_session_id: str) -> list[str]:
        status_placeholders = ",".join("?" for _ in DELIVERABLE_SESSION_STATUSES)
        rows = self._db.fetchall(
            f"""
            SELECT id
              FROM sessions
             WHERE status IN ({status_placeholders})
               AND id != ?
               AND id != ?
             ORDER BY created_at ASC, id ASC
            """,
            (*DELIVERABLE_SESSION_STATUSES, SYSTEM_SESSION_ID, from_session_id),
        )
        return self._dedupe([str(row["id"]) for row in rows])

    def _agent_recipient_session_ids(
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

        return self._dedupe_agent_recipient_rows(rows, from_session_id=from_session_id)

    def _build_recipient_session_ids(
        self,
        *,
        from_session_id: str,
        root_task_id: str,
    ) -> list[str]:
        active_status_placeholders = ",".join("?" for _ in ACTIVE_AGENT_RUN_STATUSES)
        rows = self._db.fetchall(
            f"""
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM tasks WHERE id = ?
                UNION ALL
                SELECT child.id
                  FROM tasks child
                  JOIN subtree parent ON child.parent_task_id = parent.id
            )
            SELECT
                ar.child_session_id,
                ar.parent_session_id,
                child.status AS child_status,
                parent.status AS parent_status
            FROM agent_runs ar
            JOIN subtree ON subtree.id = ar.task_id
            LEFT JOIN sessions child ON child.id = ar.child_session_id
            LEFT JOIN sessions parent ON parent.id = ar.parent_session_id
            WHERE ar.status IN ({active_status_placeholders})
            ORDER BY ar.created_at ASC
            """,
            (root_task_id, *ACTIVE_AGENT_RUN_STATUSES),
        )
        return self._dedupe_agent_recipient_rows(rows, from_session_id=from_session_id)

    def _resolve_agent_target(
        self,
        *,
        from_session_id: str,
        agent_run_id: str,
        project_id: str | None,
    ) -> MailboxTargetResolution:
        active_status_placeholders = ",".join("?" for _ in ACTIVE_AGENT_RUN_STATUSES)
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
            WHERE ar.id = ?
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

        recipient_id = self._validate_direct_recipient(
            from_session_id=from_session_id,
            to_session_id=recipient_id,
            project_id=project_id,
        )
        return MailboxTargetResolution(
            target="agent",
            target_id=agent_run_id,
            recipient_session_ids=[recipient_id],
            selector_metadata={
                "target": "agent",
                "agent_run_id": agent_run_id,
                "agent_run_status": row["status"],
                "task_id": row["task_id"],
            },
        )

    def _resolve_build_target(
        self,
        *,
        target_id: str,
        from_session_id: str,
        project_id: str | None,
    ) -> tuple[str, dict[str, Any]]:
        sender_project_id = self._resolve_project_id(from_session_id, project_id)

        run_row = self._db.fetchone(
            "SELECT id, project_id, root_task_id, input_ref FROM build_runs WHERE id = ?",
            (target_id,),
        )
        if run_row is not None:
            root_task_id = self._resolve_build_run_root_task(
                run_row=run_row,
                fallback_project_id=sender_project_id,
            )
            return root_task_id, {
                "target": "build",
                "build_run_id": str(run_row["id"]),
                "input_ref": run_row["input_ref"],
                "project_id": str(run_row["project_id"]),
                "selector_source": "build_run",
            }

        input_rows = self._db.fetchall(
            """
            SELECT id, project_id, root_task_id, input_ref
              FROM build_runs
             WHERE project_id = ?
               AND input_ref = ?
             ORDER BY started_at DESC, id DESC
             LIMIT 1
            """,
            (sender_project_id, target_id),
        )
        if input_rows:
            root_task_id = self._resolve_build_run_root_task(
                run_row=input_rows[0],
                fallback_project_id=sender_project_id,
            )
            return root_task_id, {
                "target": "build",
                "build_run_id": str(input_rows[0]["id"]),
                "input_ref": input_rows[0]["input_ref"],
                "project_id": str(input_rows[0]["project_id"]),
                "selector_source": "build_input",
            }

        try:
            root_task_id = resolve_task_reference(self._db, target_id, sender_project_id)
        except TaskNotFoundError:
            root_task_id = ""
        if root_task_id:
            task_project_id = self._task_project_id(root_task_id)
            return root_task_id, {
                "target": "build",
                "project_id": task_project_id,
                "selector_source": "root_task",
            }

        raise ValueError(f"Build target not found: {target_id}")

    def _resolve_build_run_root_task(
        self,
        *,
        run_row: Mapping[str, Any],
        fallback_project_id: str,
    ) -> str:
        root_task_id = run_row["root_task_id"]
        if root_task_id:
            return str(root_task_id)

        input_ref = run_row["input_ref"]
        if input_ref:
            try:
                return resolve_task_reference(self._db, str(input_ref), str(run_row["project_id"]))
            except TaskNotFoundError:
                try:
                    return resolve_task_reference(self._db, str(input_ref), fallback_project_id)
                except TaskNotFoundError:
                    pass
        raise ValueError(f"Build run has no resolvable root task: {run_row['id']}")

    def _task_project_id(self, task_id: str) -> str:
        row = self._db.fetchone("SELECT project_id FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise ValueError(f"Task not found: {task_id}")
        return str(row["project_id"])

    @staticmethod
    def _agent_selector_metadata(
        *,
        target: str,
        project_id: str | None,
        exclude_session_id: str,
    ) -> dict[str, Any]:
        return {
            "target": target,
            "project_id": project_id,
            "agent_run_status": list(ACTIVE_AGENT_RUN_STATUSES),
            "session_status": list(DELIVERABLE_SESSION_STATUSES),
            "exclude_session_id": exclude_session_id,
        }

    def _dedupe_agent_recipient_rows(
        self,
        rows: list[Mapping[str, Any]],
        *,
        from_session_id: str,
    ) -> list[str]:
        recipients: list[str] = []
        for row in rows:
            recipient_id = self._select_agent_recipient(
                child_id=row["child_session_id"],
                child_status=row["child_status"],
                parent_id=row["parent_session_id"],
                parent_status=row["parent_status"],
            )
            if recipient_id is None or recipient_id == from_session_id:
                continue
            recipients.append(recipient_id)
        return self._dedupe(recipients)

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            result.append(value)
            seen.add(value)
        return result

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
    ) -> str | None:
        if metadata is None and broadcast_id is None:
            return None

        payload = dict(metadata or {})
        if broadcast_id is not None:
            payload["broadcast_id"] = broadcast_id
            payload["broadcast"] = {
                "target": resolution.target,
                "target_id": resolution.target_id,
                "selector": resolution.selector_metadata,
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

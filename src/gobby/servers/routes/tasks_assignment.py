"""Task assignment notification helpers for task routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.sessions.mailbox import MailboxService
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SYSTEM_SESSION_ID

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class TaskAssignmentNotifier:
    """Lazily create mailbox dependencies for task assignment notifications."""

    def __init__(self, server: HTTPServer) -> None:
        self._server = server
        self._message_manager: InterSessionMessageManager | None = None
        self._mailbox: MailboxService | None = None

    def _mailbox_service(self) -> MailboxService | None:
        session_manager = self._server.session_manager
        if session_manager is None:
            return None
        if self._message_manager is None:
            self._message_manager = InterSessionMessageManager(self._server.services.database)
        if self._mailbox is None:
            self._mailbox = MailboxService(
                db=self._server.services.database,
                message_manager=self._message_manager,
                session_manager=session_manager,
                wake_dispatcher=self._server.services.wake_dispatcher,
            )
        return self._mailbox

    async def send(self, *, task_dict: dict[str, Any], to_session_id: str) -> None:
        """Persist and wake a task-assignment mailbox notification."""
        raw_task_id = task_dict.get("id")
        if not isinstance(raw_task_id, str) or not raw_task_id.strip():
            raise ValueError("Task assignment notification requires task id")
        if not isinstance(to_session_id, str) or not to_session_id.strip():
            raise ValueError("Task assignment notification requires target session id")
        task_id = raw_task_id.strip()
        target_session_id = to_session_id.strip()

        mailbox = self._mailbox_service()
        if mailbox is None:
            logger.debug(
                "Skipping task assignment mailbox message; session manager unavailable",
                extra={
                    "task_id": task_id,
                    "task_ref": task_dict.get("ref"),
                    "to_session_id": target_session_id,
                    "project_id": task_dict.get("project_id"),
                },
            )
            return

        raw_state = task_dict.get("state")
        state = cast(dict[str, Any], raw_state) if isinstance(raw_state, dict) else {}
        current_stage = state.get("current_stage")
        if state.get("is_closed"):
            task_status = "closed"
        elif state.get("is_escalated"):
            task_status = "escalated"
        elif isinstance(current_stage, dict) and current_stage.get("state"):
            task_status = str(current_stage["state"])
        else:
            task_status = "open"

        raw_ref = task_dict.get("ref")
        task_ref = raw_ref if isinstance(raw_ref, str) and raw_ref.strip() else "(unknown-ref)"
        raw_title = task_dict.get("title")
        task_title = raw_title if isinstance(raw_title, str) and raw_title.strip() else "(Untitled)"
        metadata = {
            "task_id": task_id,
            "task_ref": task_ref,
            "task_title": task_title,
            "task_status": task_status,
            "task_stage": current_stage,
            "assigned_session_id": target_session_id,
        }
        content = f"{task_ref} assigned: {task_title}"
        log_context = {
            "task_id": task_id,
            "task_ref": task_ref,
            "to_session_id": target_session_id,
            "project_id": task_dict.get("project_id"),
            "message_type": "task_assignment",
        }
        logger.info(
            "Sending task assignment mailbox message",
            extra=log_context,
        )
        try:
            send_result = await mailbox.send(
                from_session_id=SYSTEM_SESSION_ID,
                target="session",
                target_id=target_session_id,
                content=content,
                priority="high",
                message_type="task_assignment",
                metadata=metadata,
                project_id=cast(str | None, task_dict.get("project_id")),
                include_wakeup=True,
            )
        except Exception:
            logger.exception(
                "Failed to send task assignment mailbox message",
                extra=log_context,
            )
            raise
        logger.info(
            "Sent task assignment mailbox message",
            extra={
                **log_context,
                "message_ids": send_result.message_ids,
                "wake_results": send_result.wake_results,
            },
        )

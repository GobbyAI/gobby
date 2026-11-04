"""Constants for agent run storage."""

from __future__ import annotations

import logging
from typing import Literal, NewType

AgentRunStatus = Literal["queued", "pending", "running", "success", "error", "timeout", "cancelled"]
# ``task_closed:<close reason>``: close reasons are free text, so the seat's
# waiters see the reason the task actually closed with.
TaskClosedTerminalReason = NewType("TaskClosedTerminalReason", str)
AgentRunTerminalReason = (
    Literal[
        "agent_definition_unavailable",
        "review_verdict_missing",
        "user_cancelled",
        "daemon_stop",
        "task_completed",
        "spawn_rollback",
        "task_blocker",
        "early_exit",
        "provider_quota_exhausted",
        "provider_error",
    ]
    | TaskClosedTerminalReason
)
TerminalAction = Literal["complete", "fail", "timeout", "cancel"]


def task_closed_terminal_reason(closed_reason: str | None) -> TaskClosedTerminalReason:
    """The terminal reason for a seat torn down because its task closed."""
    reason = (closed_reason or "").strip() or "unspecified"
    return TaskClosedTerminalReason(f"task_closed:{reason}")


STATUS_QUEUED: AgentRunStatus = "queued"
STATUS_PENDING: AgentRunStatus = "pending"
STATUS_RUNNING: AgentRunStatus = "running"
STATUS_SUCCESS: AgentRunStatus = "success"
STATUS_ERROR: AgentRunStatus = "error"
STATUS_TIMEOUT: AgentRunStatus = "timeout"
STATUS_CANCELLED: AgentRunStatus = "cancelled"

ACTIVE_AGENT_RUN_STATUSES: tuple[AgentRunStatus, ...] = (
    STATUS_QUEUED,
    STATUS_PENDING,
    STATUS_RUNNING,
)
ACTIVE_AGENT_RUN_STATUS_SQL = ", ".join(f"'{status}'" for status in ACTIVE_AGENT_RUN_STATUSES)
TERMINAL_AGENT_RUN_STATUSES: tuple[AgentRunStatus, ...] = (
    STATUS_SUCCESS,
    STATUS_ERROR,
    STATUS_TIMEOUT,
    STATUS_CANCELLED,
)

logger = logging.getLogger("gobby.storage.agents")

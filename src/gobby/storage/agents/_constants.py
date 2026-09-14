"""Constants for agent run storage."""

from __future__ import annotations

import logging
from typing import Literal

AgentRunStatus = Literal["pending", "running", "success", "error", "timeout", "cancelled"]
AgentRunTerminalReason = Literal[
    "user_cancelled",
    "daemon_stop",
    "task_completed",
    "spawn_rollback",
    "task_blocker",
    "early_exit",
    "provider_quota_exhausted",
    "provider_error",
]

# Terminal reasons where we ended the run on purpose, rather than the runtime
# ending it under us. Only these say nothing about the provider's health, so a
# retry belongs on the same candidate; every other unfinished ending — a
# classified provider error, or one the runtime never classified at all —
# is a reason to try somewhere else.
DELIBERATE_STOP_TERMINAL_REASONS: tuple[AgentRunTerminalReason, ...] = (
    "user_cancelled",
    "daemon_stop",
    "spawn_rollback",
)

# SESSION_END uses this prefix when a step workflow is still open. Closed-task
# reconciliation may revive an error that starts with this string.
INCOMPLETE_STEP_WORKFLOW_ERROR = "Agent session ended before step workflow completed"

STATUS_PENDING: AgentRunStatus = "pending"
STATUS_RUNNING: AgentRunStatus = "running"
STATUS_SUCCESS: AgentRunStatus = "success"
STATUS_ERROR: AgentRunStatus = "error"
STATUS_TIMEOUT: AgentRunStatus = "timeout"
STATUS_CANCELLED: AgentRunStatus = "cancelled"

ACTIVE_AGENT_RUN_STATUSES: tuple[AgentRunStatus, ...] = (STATUS_PENDING, STATUS_RUNNING)
ACTIVE_AGENT_RUN_STATUS_SQL = ", ".join(f"'{status}'" for status in ACTIVE_AGENT_RUN_STATUSES)
TERMINAL_AGENT_RUN_STATUSES: tuple[AgentRunStatus, ...] = (
    STATUS_SUCCESS,
    STATUS_ERROR,
    STATUS_TIMEOUT,
    STATUS_CANCELLED,
)

logger = logging.getLogger("gobby.storage.agents")

"""Constants for agent run storage."""

from __future__ import annotations

import logging
from typing import Literal

AgentRunStatus = Literal["pending", "running", "success", "error", "timeout", "cancelled"]
AgentRunTerminalReason = Literal[
    "user_cancelled", "daemon_stop", "task_completed", "spawn_rollback"
]

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

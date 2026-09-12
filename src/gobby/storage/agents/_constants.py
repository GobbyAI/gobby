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
    "provider_quota_exhausted",
    "provider_error",
]

# Terminal reasons where the provider, not the work, ended the run. Nothing
# about the run's subject changes by retrying it, so a retry is only worth
# making against a different provider.
PROVIDER_FAILURE_TERMINAL_REASONS: tuple[AgentRunTerminalReason, ...] = (
    "provider_quota_exhausted",
    "provider_error",
)

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

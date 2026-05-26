"""Constants for agent run storage."""

from __future__ import annotations

import logging
import sys
from typing import Literal, cast

AgentRunStatus = Literal["pending", "running", "success", "error", "timeout", "cancelled"]
AgentRunTerminalReason = Literal["user_cancelled", "daemon_restart", "daemon_stop"]

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


def get_logger() -> logging.Logger:
    """Return the package logger, respecting tests that patch the public attribute."""
    package = sys.modules.get("gobby.storage.agents")
    if package is not None:
        public_logger = getattr(package, "logger", None)
        if public_logger is not None:
            return cast(logging.Logger, public_logger)
    return logger

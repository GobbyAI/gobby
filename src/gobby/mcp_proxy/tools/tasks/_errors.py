"""Structured error helpers for task MCP tools."""

from enum import Enum
from typing import Any


class TaskToolErrorCode(str, Enum):
    """Stable task tool error codes for workflow branching."""

    TASK_CLOSED = "TASK_CLOSED"
    TASK_INVALID_STATUS = "TASK_INVALID_STATUS"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    SESSION_REQUIRED = "SESSION_REQUIRED"
    TASK_CLAIM_CONFLICT = "TASK_CLAIM_CONFLICT"


def task_error(error: str, code: TaskToolErrorCode, **details: Any) -> dict[str, Any]:
    """Return a structured task MCP failure while preserving human-readable text."""
    return {
        "success": False,
        "status": "error",
        "error": error,
        "error_code": code.value,
        **details,
    }

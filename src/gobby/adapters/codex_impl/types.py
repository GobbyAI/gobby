"""
Type definitions and data classes for Codex adapter.

Extracted from codex.py as part of Phase 3 Strangler Fig decomposition.
These types are used throughout the Codex adapter implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CodexConnectionState(Enum):
    """Connection state for the Codex app-server."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class CodexThread:
    """Represents a Codex conversation thread."""

    id: str
    preview: str = ""
    model_provider: str = "openai"
    created_at: int = 0
    path: str | None = None


@dataclass
class CodexTurn:
    """Represents a turn in a Codex conversation."""

    id: str
    thread_id: str
    status: str = "pending"
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class CodexItem:
    """Represents an item (message, tool call, etc.) in a turn."""

    id: str
    type: str  # "reasoning", "agent_message", "command_execution", "user_message", etc.
    content: str = ""
    status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)


# Type alias for notification handlers
NotificationHandler = Callable[[str, dict[str, Any]], None]


__all__ = [
    "CodexConnectionState",
    "CodexThread",
    "CodexTurn",
    "CodexItem",
    "NotificationHandler",
]

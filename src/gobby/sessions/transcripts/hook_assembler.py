"""
Hook-based transcript assembler for CLIs without transcript files.

Some CLI integrations don't write local transcript files. This module
converts HookEvent objects into ParsedMessage objects as they flow
through HookManager.handle(), enabling transcript reconstruction
from hook events alone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType
from gobby.sessions.transcripts.base import ParsedMessage

logger = logging.getLogger(__name__)


class HookTranscriptAssembler:
    """Assembles transcripts from hook events for CLIs without transcript files.

    Maintains per-session message indices and converts each relevant
    HookEvent into one or more ParsedMessage objects for storage.
    """

    def __init__(self) -> None:
        self._message_indices: dict[str, int] = {}  # session_id -> next index

    def _next_index(self, session_id: str) -> int:
        """Get and increment the message index for a session."""
        idx = self._message_indices.get(session_id, 0)
        self._message_indices[session_id] = idx + 1
        return idx

    def _message_id(self, session_id: str, index: int, raw_data: dict[str, Any]) -> str:
        """Return a stable hook-derived message identifier."""
        for key in ("message_id", "messageId", "id"):
            value = raw_data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return f"{session_id}:hook:{index}"

    def process_event(self, session_id: str, event: HookEvent) -> list[ParsedMessage]:
        """Process a hook event, returning any messages to store.

        Args:
            session_id: Platform (Gobby) session ID.
            event: The unified HookEvent from an adapter.

        Returns:
            List of ParsedMessage objects to store (usually 0 or 1).
        """
        handler = _EVENT_HANDLERS.get(event.event_type)
        if handler is None:
            return []
        return handler(self, session_id, event)

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    def _handle_before_agent(self, session_id: str, event: HookEvent) -> list[ParsedMessage]:
        """BEFORE_AGENT → user message (the prompt that triggered the agent)."""
        content = (
            event.data.get("user_input")
            or event.data.get("prompt")
            or event.data.get("content")
            or ""
        )
        if not content:
            return []
        return [
            self._make_message(
                session_id=session_id,
                role="user",
                content=content,
                content_type="text",
                timestamp=event.timestamp,
                raw_data=event.data,
            )
        ]

    def _handle_after_agent(self, session_id: str, event: HookEvent) -> list[ParsedMessage]:
        """AFTER_AGENT → assistant text message."""
        content = event.data.get("response") or event.data.get("content") or ""
        if not content:
            return []
        return [
            self._make_message(
                session_id=session_id,
                role="assistant",
                content=content,
                content_type="text",
                timestamp=event.timestamp,
                raw_data=event.data,
            )
        ]

    def _handle_before_tool(self, session_id: str, event: HookEvent) -> list[ParsedMessage]:
        """BEFORE_TOOL → tool_use message."""
        tool_name = event.data.get("tool_name") or event.data.get("toolName") or "unknown"
        tool_input = (
            event.data.get("tool_input")
            or event.data.get("toolArgs")
            or event.data.get("input")
            or {}
        )
        if isinstance(tool_input, str):
            tool_input = {"raw": tool_input}
        return [
            self._make_message(
                session_id=session_id,
                role="assistant",
                content=f"Using tool: {tool_name}",
                content_type="tool_use",
                timestamp=event.timestamp,
                raw_data=event.data,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        ]

    def _handle_after_tool(self, session_id: str, event: HookEvent) -> list[ParsedMessage]:
        """AFTER_TOOL → tool_result message."""
        tool_name = event.data.get("tool_name") or event.data.get("toolName") or "unknown"
        # Extract tool output from various possible field names
        tool_output = (
            event.data.get("tool_output")
            or event.data.get("tool_result")
            or event.data.get("output")
            or {}
        )
        # Some CLIs nest result under toolResult.textResultForLlm
        tool_result_obj = event.data.get("toolResult")
        if isinstance(tool_result_obj, dict):
            text_result = tool_result_obj.get("textResultForLlm")
            if text_result:
                tool_output = {"text": text_result}

        if isinstance(tool_output, str):
            tool_output = {"text": tool_output}

        content = ""
        if isinstance(tool_output, dict):
            content = tool_output.get("text", "") or tool_output.get("raw", "")

        return [
            self._make_message(
                session_id=session_id,
                role="tool",
                content=content,
                content_type="tool_result",
                timestamp=event.timestamp,
                raw_data=event.data,
                tool_name=tool_name,
                tool_result=tool_output,
            )
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        content_type: str,
        timestamp: datetime,
        raw_data: dict[str, Any],
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_result: dict[str, Any] | None = None,
    ) -> ParsedMessage:
        """Build a ParsedMessage with auto-incrementing index."""
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        index = self._next_index(session_id)
        return ParsedMessage(
            index=index,
            role=role,
            content=content,
            content_type=content_type,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            timestamp=timestamp,
            raw_json=raw_data,
            message_id=self._message_id(session_id, index, raw_data),
        )


# Dispatch table — avoids long if/elif chains
_EVENT_HANDLERS: dict[
    HookEventType,
    Callable[[HookTranscriptAssembler, str, HookEvent], list[ParsedMessage]],
] = {
    HookEventType.BEFORE_AGENT: HookTranscriptAssembler._handle_before_agent,
    HookEventType.AFTER_AGENT: HookTranscriptAssembler._handle_after_agent,
    HookEventType.BEFORE_TOOL: HookTranscriptAssembler._handle_before_tool,
    HookEventType.AFTER_TOOL: HookTranscriptAssembler._handle_after_tool,
}

"""Read authenticated durable message rows for declared workflow handlers."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessage, InterSessionMessageManager


def result_body(output: Any) -> dict[str, Any]:
    """Unwrap the normalized MCP result, without trusting supplied tool arguments."""
    if not isinstance(output, dict):
        return {}
    body = output.get("result")
    return body if isinstance(body, dict) else output


def completion_messages(
    db: HubDatabase,
    session_id: str,
    tool: str,
    tool_input: dict[str, Any],
    tool_output: Any,
) -> list[InterSessionMessage]:
    """Load successful sends or explicitly read incoming messages from storage."""
    ids: list[str] = []
    if tool == "send_message":
        values = result_body(tool_output).get("message_ids")
        if isinstance(values, list):
            ids = [value for value in values if isinstance(value, str)]
    elif tool == "get_inter_session_message":
        value = tool_input.get("message_id")
        if isinstance(value, str):
            ids = [value]
    manager = InterSessionMessageManager(db)
    messages: list[InterSessionMessage] = []
    for message_id in ids:
        message = manager.get_message(message_id)
        if message is None or message.from_session == message.to_session:
            continue
        if tool == "send_message" and message.from_session == session_id:
            messages.append(message)
        elif tool == "get_inter_session_message" and message.to_session == session_id:
            messages.append(message)
    return messages

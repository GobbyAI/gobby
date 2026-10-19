"""Translate successful native Codex file additions into confirmed tool edits."""

from datetime import datetime
from typing import Any

from gobby.sessions.transcripts.base import ParsedToolEvent


def file_change_events(data: dict[str, Any], cwd: str | None) -> list[ParsedToolEvent]:
    """Preserve full added source only from a completed native FileChange receipt.

    Updates contain patches rather than full source. Neither those nor incomplete
    receipts can establish the source that existed before a validation run.
    """
    if data.get("type") != "event_msg":
        return []
    payload = data.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return []
    item = payload.get("item")
    if not isinstance(item, dict):
        return []
    if item.get("type") != "FileChange" or item.get("status") != "completed":
        return []
    item_id = item.get("id")
    changes = item.get("changes")
    stamp = data.get("timestamp")
    if not isinstance(item_id, str) or not item_id.strip() or not isinstance(changes, dict):
        return []
    if not isinstance(stamp, str):
        return []
    try:
        timestamp = datetime.fromisoformat(stamp)
    except ValueError:
        return []
    if timestamp.tzinfo is None:
        return []
    events: list[ParsedToolEvent] = []
    for path, change in changes.items():
        if not isinstance(path, str) or not path.strip() or not isinstance(change, dict):
            continue
        content = change.get("content")
        if change.get("type") != "add" or not isinstance(content, str):
            continue
        arguments: dict[str, Any] = {"file_path": path, "content": content}
        if cwd is not None:
            arguments["cwd"] = cwd
        call_id = f"codex-file-add:{item_id}:{path}"
        for phase in ("begin", "end"):
            events.append(
                ParsedToolEvent(
                    phase=phase,
                    call_id=call_id,
                    server=None,
                    tool="write",
                    arguments=arguments,
                    timestamp=timestamp,
                    raw_json=data,
                    result={"success": True} if phase == "end" else None,
                )
            )
    return events

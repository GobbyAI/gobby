"""Dispatcher audit marker helpers."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._updates import update_task


def audit_marker_text(heading: str, body: str) -> str:
    return f"\n\n### {heading}\n\n{body}"


def append_audit_marker(db: HubDatabase, task_id: str, heading: str, body: str) -> bool:
    task = get_task(db, task_id)
    description = task.description or ""
    marker = audit_marker_text(heading, body)
    if marker in description:
        return False
    update_task(db, task_id, description=f"{description}{marker}")
    return True

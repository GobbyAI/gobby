"""Dispatcher audit marker helpers."""

from __future__ import annotations

import asyncio
import logging
import re

import psycopg

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._updates import update_task

logger = logging.getLogger(__name__)


def audit_marker_text(heading: str, body: str) -> str:
    """Build the Markdown audit marker appended to a task description.

    Args:
        heading: Marker heading without the leading ``###``.
        body: Marker body text. Callers should pass text without relying on a
            trailing newline.

    Returns:
        A marker string that starts with two newlines and formats as
        ``### {heading}``, a blank line, then ``body``.

    Example:
        ``audit_marker_text("Dispatch", "failed")`` returns
        ``"\n\n### Dispatch\n\nfailed"``.
    """
    return f"\n\n### {heading}\n\n{body}"


async def append_audit_marker(db: HubDatabase, task_id: str, heading: str, body: str) -> bool:
    try:
        task = await asyncio.to_thread(get_task, db, task_id)
        description = task.description or ""
        marker = audit_marker_text(heading, body)
        if re.search(rf"{re.escape(marker)}\s*$", description):
            return False
        await asyncio.to_thread(update_task, db, task_id, description=f"{description}{marker}")
        return True
    except (ValueError, psycopg.Error):
        logger.warning("Failed to append dispatch audit marker for task %s", task_id, exc_info=True)
        return False

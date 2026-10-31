"""Restore the full result behind an offload envelope for step evaluation."""

from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from gobby.config.features import ToolResultOffloadConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tool_results import ToolResultStore

logger = logging.getLogger(__name__)


def restore_offloaded_output(
    db: HubDatabase,
    tool_output: Any,
    session_id: str | None,
    project_id: str | None,
) -> Any:
    """Return the stored result an offload envelope points at, else the input unchanged.

    The model keeps receiving the bounded envelope. Step handlers need the full
    result, because an envelope drops nested fields such as step_records.
    """
    if not isinstance(tool_output, dict):
        return tool_output
    holder = tool_output.get("result")
    if not isinstance(holder, dict):
        holder = tool_output
    if not _is_retrievable_envelope(holder):
        return tool_output
    try:
        project = project_id or _session_project_id(db, session_id)
        if not project:
            return tool_output
        store = ToolResultStore(db, ToolResultOffloadConfig())
        text = store.get_text(holder["result_id"], project)
        restored = json.loads(text) if text is not None else None
    except (ValueError, psycopg.Error):
        logger.warning(
            "Could not restore offloaded tool result for step evaluation; using envelope",
            exc_info=True,
        )
        return tool_output
    if restored is None:
        return tool_output
    if holder is tool_output:
        return restored
    return {**tool_output, "result": restored}


def _session_project_id(db: HubDatabase, session_id: str | None) -> str | None:
    if not session_id:
        return None
    session = SessionManager(db).get(session_id)
    return session.project_id if session else None


def _is_retrievable_envelope(value: dict[str, Any]) -> bool:
    return (
        value.get("offloaded") is True
        and value.get("retrieval_available") is True
        and value.get("content_kind") == "json"
        and isinstance(value.get("result_id"), str)
    )

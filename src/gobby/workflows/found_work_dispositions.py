"""Evidence-bound owner dispositions for another session's validation failure."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence, Set
from datetime import datetime
from typing import Any

from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES

logger = logging.getLogger(__name__)
_HOLD_LABELS = frozenset({"needs-decision", "needs-planning", "clean-window"})


def has_owner_filed_disposition(
    *,
    session_manager: Any,
    session_task_manager: Any,
    originating_session_id: str,
    failure_paths: Set[str],
    failure_at: datetime,
) -> bool:
    """Credit a linked same-finding task only when a live owner accepted responsibility."""
    if session_manager is None or session_task_manager is None or not failure_paths:
        return False
    try:
        links = session_task_manager.get_session_tasks(originating_session_id)
    except Exception:
        logger.debug("Could not inspect owner-filed found-work tasks", exc_info=True)
        return False

    for link in links:
        if not isinstance(link, Mapping) or link.get("action") != "discovered":
            continue
        task = link.get("task")
        creator_id = getattr(task, "created_in_session_id", None)
        if not isinstance(creator_id, str) or creator_id == originating_session_id:
            continue
        created_at = getattr(task, "created_at", None)
        linked_at = link.get("link_created_at")
        if not isinstance(created_at, datetime) or not isinstance(linked_at, datetime):
            continue
        if created_at < failure_at or linked_at < created_at:
            continue
        if getattr(task, "closed_at", None) is not None:
            continue
        summary = f"{getattr(task, 'title', '')} {getattr(task, 'description', '')}"
        if not any(_contains_path(summary, path) for path in failure_paths):
            continue

        try:
            creator = session_manager.get(creator_id)
        except Exception:
            logger.debug("Could not inspect found-work task filer", exc_info=True)
            continue
        if creator is None or getattr(creator, "status", None) not in LIVE_SESSION_STATUSES:
            continue
        labels = getattr(task, "labels", None)
        if isinstance(labels, Sequence) and not isinstance(labels, str | bytes):
            if _HOLD_LABELS.intersection(labels):
                return True
        target_id = getattr(task, "delegated_to_session_id", None)
        if isinstance(target_id, str):
            try:
                receiver = session_manager.get(target_id)
            except Exception:
                logger.debug("Could not inspect found-work delegation receiver", exc_info=True)
                continue
            if receiver is not None and getattr(receiver, "status", None) in LIVE_SESSION_STATUSES:
                return True
    return False


def _contains_path(summary: str, path: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(path)}(?![\w./-])", summary) is not None

"""Archive superseded inbox hooks before entering live hook execution."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from gobby.storage.sessions import TERMINAL_SESSION_STATUSES, SessionManager

logger = logging.getLogger(__name__)


class ArchiveHook(Protocol):
    def __call__(self, path: Path, *, reason: str, detail: str) -> bool: ...


def _event_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def archive_superseded_hook(
    app: Any, envelope: dict[str, Any], path: Path, archive: ArchiveHook
) -> bool | None:
    """Return None for admissible replay, otherwise the archive outcome.

    Called off-loop before replay mutates any hook effects. Delivery receipts
    bypass this fence: they acknowledge an exact receipt generation, not a turn.
    Unknown sessions remain admissible so a first SessionStart can register them.
    A known terminal session is never reopened by a delayed inbox hook.
    """
    manager = getattr(getattr(app, "state", None), "hook_manager", None)
    session_manager = getattr(manager, "session_manager", None)
    if session_manager is None:
        return None
    sessions = cast(SessionManager, session_manager)
    data = envelope.get("input_data")
    data = data if isinstance(data, dict) else {}
    context = data.get("terminal_context")
    context = context if isinstance(context, dict) else {}
    headers = envelope.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    session_id = next(
        (
            value
            for key, value in headers.items()
            if isinstance(key, str)
            and key.lower() == "x-gobby-session-id"
            and isinstance(value, str)
            and value
        ),
        context.get("gobby_session_id"),
    )
    try:
        session = sessions.get(session_id) if isinstance(session_id, str) and session_id else None
        if session is None:
            external_id, source = data.get("session_id"), envelope.get("source")
            if isinstance(external_id, str) and external_id and isinstance(source, str):
                session = sessions.find_by_external_id_any_project(external_id, source)
    except Exception:
        # A failed lookup cannot prove admission. Retain for the next pass,
        # instead of running potentially stale effects during a hub outage.
        logger.exception("Cannot establish hook replay freshness for %s", path.name)
        return False
    if session is None:
        return None
    enqueued_at = _event_time(envelope.get("enqueued_at"))
    if session.status in TERMINAL_SESSION_STATUSES:
        detail = "session is terminal; delayed replay cannot revive it"
    elif enqueued_at is None:
        detail = "known session but no valid envelope event time to establish freshness"
    elif isinstance(session.last_activity, datetime):
        activity = session.last_activity
        activity = (
            activity.replace(tzinfo=UTC) if activity.tzinfo is None else activity.astimezone(UTC)
        )
        if activity <= enqueued_at:
            return None
        detail = "envelope predates confirmed session activity"
    else:
        return None
    return archive(path, reason="superseded_hook", detail=detail)

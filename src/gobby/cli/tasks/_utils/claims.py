"""Active-session claim resolution for task CLI rendering."""

import logging

import psycopg

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def get_claimed_task_owners(db: HubDatabase) -> dict[str, str]:
    """Map active-session claimed task IDs to their owner session IDs.

    Reads canonical task claims whose owner sessions are still active.

    Returns:
        Mapping of task UUID to active owner session UUID.
    """
    try:
        rows = db.fetchall(
            """
            SELECT t.id, t.claimed_by_session_id
            FROM tasks t
            JOIN sessions s ON s.id = t.claimed_by_session_id
            WHERE t.claimed_by_session_id IS NOT NULL
              AND s.status = 'active'
            """
        )
        return {str(row["id"]): str(row["claimed_by_session_id"]) for row in rows}
    except (RuntimeError, KeyError, psycopg.Error) as e:
        logger.debug("Failed to get claimed task owners: %s", e, exc_info=True)
        return {}


def get_claimed_task_ids(db: HubDatabase) -> set[str]:
    """Get task IDs that are claimed by active sessions."""
    return set(get_claimed_task_owners(db))

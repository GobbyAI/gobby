"""Durable proof that a deferred SessionStart was a CLI's own startup (#25647).

A startup SessionStart with no row defers it to the thread's first activity hook, and
the daemon may restart in between. The mark records that a real startup began the
thread, so the first registration may search for its fresh seat; no other start
(a /clear successor, a compaction, a synthetic start) ever writes one.
"""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase

# A seat can sit at its first prompt over a weekend; an unconsumed mark only lets that
# thread's own first registration search, so a long age risks nothing but a row.
STARTUP_MARK_MAX_AGE_HOURS = 7 * 24


def record_startup_mark(db: HubDatabase, *, machine_id: str, source: str, external_id: str) -> None:
    """Record that ``external_id`` began with its CLI's own startup on ``machine_id``."""
    db.execute(
        """
        INSERT INTO session_startup_marks (machine_id, source, external_id)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (machine_id, source, external_id),
    )


def startup_mark_exists(db: HubDatabase, *, machine_id: str, source: str, external_id: str) -> bool:
    """Whether a real startup began ``external_id`` and no registration consumed it."""
    row = db.fetchone(
        """
        SELECT 1 AS marked FROM session_startup_marks
        WHERE machine_id = %s AND source = %s AND external_id = %s
        """,
        (machine_id, source, external_id),
    )
    return row is not None


def delete_startup_mark(db: HubDatabase, *, machine_id: str, source: str, external_id: str) -> None:
    """Consume the thread's mark once its first registration stored the row."""
    db.execute(
        """
        DELETE FROM session_startup_marks
        WHERE machine_id = %s AND source = %s AND external_id = %s
        """,
        (machine_id, source, external_id),
    )


def prune_startup_marks(db: HubDatabase, *, max_age_hours: int = STARTUP_MARK_MAX_AGE_HOURS) -> int:
    """Delete marks older than ``max_age_hours`` that no registration consumed."""
    cursor = db.execute(
        """
        DELETE FROM session_startup_marks
        WHERE created_at < clock_timestamp() - make_interval(hours => %s)
        """,
        (max_age_hours,),
    )
    return cursor.rowcount

"""Durable proof that a deferred SessionStart was a CLI's own startup (#25647).

A startup SessionStart with no row defers it to the thread's first activity hook, and
the daemon may restart in between. The mark records that a real startup began the
thread, so the first registration may search for its fresh seat; no other start
(a /clear successor, a compaction, a synthetic start) ever writes one.

A mark may also pin the CLI process the startup's kernel-attested hook came from, by pid
and start time (#25905). Only that CLI's hook may then create the row and receive its
credential. The first pin stands: it is never evicted or overwritten, and it is replaced
only when the caller proves the pinned process dead and names it exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase

# A seat can sit at its first prompt over a weekend; an unconsumed mark only lets that
# thread's own first registration search, so a long age risks nothing but a row.
STARTUP_MARK_MAX_AGE_HOURS = 7 * 24


@dataclass(frozen=True)
class StartupPin:
    """The CLI process a deferred startup proved: its pid and that process's start time."""

    cli_pid: int
    cli_create_time: float


@dataclass(frozen=True)
class StartupMark:
    """An unconsumed startup mark and the CLI it pins, if any."""

    pin: StartupPin | None


def startup_mark(
    db: HubDatabase, *, machine_id: str, source: str, external_id: str
) -> StartupMark | None:
    """The thread's unconsumed startup mark, or None when no real startup left one."""
    row = db.fetchone(
        """
        SELECT cli_pid, cli_create_time FROM session_startup_marks
        WHERE machine_id = %s AND source = %s AND external_id = %s
        """,
        (machine_id, source, external_id),
    )
    if row is None:
        return None
    if row["cli_pid"] is None:
        return StartupMark(pin=None)
    return StartupMark(pin=StartupPin(int(row["cli_pid"]), float(row["cli_create_time"])))


def pin_startup(
    db: HubDatabase,
    *,
    machine_id: str,
    source: str,
    external_id: str,
    pin: StartupPin,
    displaced: StartupPin | None = None,
) -> bool:
    """Pin ``pin`` to the thread's existing mark; return whether the mark now holds it.

    Without ``displaced`` only an unpinned mark takes the pin, so the first writer wins.
    With it, the stored pin is replaced only if it is exactly ``displaced``, a process the
    caller proved dead, so of two racing replacements one wins. No mark is ever created.
    """
    key = (machine_id, source, external_id)
    if displaced is None:
        db.execute(
            """
            UPDATE session_startup_marks SET cli_pid = %s, cli_create_time = %s
            WHERE machine_id = %s AND source = %s AND external_id = %s AND cli_pid IS NULL
            """,
            (pin.cli_pid, pin.cli_create_time, *key),
        )
    else:
        db.execute(
            """
            UPDATE session_startup_marks
            SET cli_pid = %s, cli_create_time = %s, created_at = clock_timestamp()
            WHERE machine_id = %s AND source = %s AND external_id = %s
              AND cli_pid = %s AND cli_create_time = %s
            """,
            (pin.cli_pid, pin.cli_create_time, *key, displaced.cli_pid, displaced.cli_create_time),
        )
    mark = startup_mark(db, machine_id=machine_id, source=source, external_id=external_id)
    return mark is not None and mark.pin == pin


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

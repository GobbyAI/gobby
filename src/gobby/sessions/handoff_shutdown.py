"""Fence local daemon shutdown against live, unconsumed session handoffs."""

from collections.abc import Iterator
from contextlib import contextmanager

from gobby.storage.hub.protocol import HubDatabase, Transaction

_closed_machines: set[str] = set()


class HandoffShutdownBlocked(RuntimeError):
    """A handoff or competing lifecycle operation prevents shutdown."""


def lock_handoff_staging(conn: Transaction, machine_id: str) -> None:
    """Hold shared admission until the handoff staging transaction commits."""
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock_shared(hashtextextended(%s, 0)) AS acquired",
        (f"gobby:handoff-shutdown:{machine_id}",),
    ).fetchone()
    if not row or not row["acquired"] or machine_id in _closed_machines:
        raise HandoffShutdownBlocked(
            "Daemon shutdown is in progress; retry set_handoff after restart. Nothing was staged."
        )


@contextmanager
def guard_handoff_shutdown(db: HubDatabase, machine_id: str) -> Iterator[None]:
    """Keep admission closed across a caller's shutdown, refusing live handoffs."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired",
            (f"gobby:handoff-shutdown:{machine_id}",),
        ).fetchone()
        if not row or not row["acquired"]:
            raise HandoffShutdownBlocked("Handoff staging or another shutdown is in progress")
        rows = conn.execute(
            """
            SELECT s.seq_num, s.id,
                   v.variables -> 'set_handoff_pending' ->> 'attempt_id' AS attempt_id
              FROM sessions s JOIN session_variables v ON v.session_id = s.id
             WHERE s.machine_id = %s
               AND jsonb_typeof(v.variables -> 'set_handoff_pending') = 'object'
               AND (
                   s.status NOT IN ('expired', 'deleted')
                   OR EXISTS (
                       SELECT 1 FROM sessions successor
                        WHERE successor.id::text =
                              v.variables -> 'clear_attempt' ->> 'consumed_by'
                          AND successor.status NOT IN ('expired', 'deleted')
                   )
               )
             ORDER BY s.seq_num
            """,
            (machine_id,),
        ).fetchall()
        if rows:
            attempts = ", ".join(
                f"#{row['seq_num']} ({row['attempt_id'] or 'unknown attempt'})" for row in rows
            )
            raise HandoffShutdownBlocked(
                f"Unresolved set_handoff in session(s) {attempts}. "
                "Wait for delivery and get_handoff acknowledgment before stopping the daemon."
            )
        yield


def prepare_handoff_shutdown(db: HubDatabase, machine_id: str) -> None:
    """Close this daemon's admission atomically with the pending-handoff check."""
    prepared = False
    try:
        with guard_handoff_shutdown(db, machine_id):
            if machine_id in _closed_machines:
                raise HandoffShutdownBlocked("Daemon shutdown is already in progress")
            _closed_machines.add(machine_id)
            prepared = True
    except BaseException:
        if prepared:
            cancel_handoff_shutdown(machine_id)
        raise


def cancel_handoff_shutdown(machine_id: str) -> None:
    """Reopen admission if HTTP shutdown failed before requesting process exit."""
    _closed_machines.discard(machine_id)

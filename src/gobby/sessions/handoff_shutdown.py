"""Fence local daemon shutdown against live, unconsumed session handoffs."""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)

# How long a staged handoff can still move on its own. The compact or clear
# keystrokes, the successor bind and the pull prompt land within seconds, and the
# awaiting_handoff sweep expires a boundary that never completed after this long.
# An older marker needs a person to submit get_handoff, and it survives a restart
# in Postgres, so it must not fence the daemon.
HANDOFF_IN_FLIGHT_MINUTES = 30

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
    """Keep admission closed across a caller's shutdown, refusing live handoffs.

    A marker that last moved more than ``HANDOFF_IN_FLIGHT_MINUTES`` ago is stale:
    nothing automatic will consume it and a restart loses nothing it needs, so it is
    logged and skipped instead of holding the daemon up.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired",
            (f"gobby:handoff-shutdown:{machine_id}",),
        ).fetchone()
        if not row or not row["acquired"]:
            raise HandoffShutdownBlocked("Handoff staging or another shutdown is in progress")
        rows = conn.execute(
            """
            SELECT s.seq_num, s.id, s.project_id, p.name AS project_name,
                   v.variables -> 'set_handoff_pending' ->> 'attempt_id' AS attempt_id,
                   v.variables -> 'set_handoff_pending' ->> 'dispatch_started_at'
                       AS dispatch_started_at,
                   v.variables -> 'set_handoff_pending' ->> 'created_at' AS created_at
              FROM sessions s JOIN session_variables v ON v.session_id = s.id
              LEFT JOIN projects p ON p.id = s.project_id
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
        cutoff = utc_now() - timedelta(minutes=HANDOFF_IN_FLIGHT_MINUTES)
        blocking: list[str] = []
        for row in rows:
            attempt = f"{_session_label(row)} ({row['attempt_id'] or 'unknown attempt'})"
            moved_at = _marker_last_moved_at(row)
            if moved_at is not None and moved_at <= cutoff:
                logger.warning(
                    "Ignoring stale set_handoff marker in %s: it last moved at %s and nothing "
                    "automatic will consume it; get_handoff still reads it after the restart",
                    attempt,
                    moved_at.isoformat(),
                )
                continue
            blocking.append(attempt)
        if blocking:
            raise HandoffShutdownBlocked(
                f"Unresolved set_handoff in session(s) {', '.join(blocking)}. "
                "Wait for delivery and get_handoff acknowledgment before stopping the daemon."
            )
        yield


def _session_label(row: Mapping[str, Any]) -> str:
    if row["seq_num"] is None:
        return str(row["id"])
    project = (row["project_name"] or "").strip() or row["project_id"]
    return f"{project}#{row['seq_num']}"


def _marker_last_moved_at(row: Mapping[str, Any]) -> datetime | None:
    """When the attempt last progressed: its delivery claim, else its staging."""
    for field in ("dispatch_started_at", "created_at"):
        value = row[field]
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
    return None


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

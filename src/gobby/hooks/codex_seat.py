"""Database ownership check for adopting a Codex seat TUI for a new thread."""

from __future__ import annotations

from datetime import UTC, datetime

from gobby.hooks.terminal_context import SeatAvailable
from gobby.storage.hub.protocol import HubDatabase

# Same tolerance as terminal_ownership.recorded_process_is_alive.
_CREATE_TIME_TOLERANCE_SECONDS = 1.0


def codex_seat_available(db: HubDatabase, machine_id: str) -> SeatAvailable:
    """Return a check that no Codex terminal session owns, or may own, a seat TUI.

    A session of any status that recorded the TUI's pid and create time owns it. A
    terminal session with no recorded process, created after the TUI started, may be an
    earlier thread of that same TUI, so it makes the seat ambiguous too.
    """

    def available(pid: int, create_time: float) -> bool:
        rows = db.fetchall(
            """
            SELECT terminal_context->>'parent_pid' AS parent_pid,
                   terminal_context->>'parent_create_time' AS parent_create_time
            FROM sessions
            WHERE source = 'codex' AND machine_id = %s AND session_type = 'terminal'
              AND (
                terminal_context->>'parent_pid' = %s
                OR (
                  NULLIF(terminal_context->>'parent_pid', '') IS NULL
                  AND created_at > %s
                )
              )
            """,
            (machine_id, str(pid), datetime.fromtimestamp(create_time, UTC)),
        )
        for row in rows:
            if not row["parent_pid"]:
                return False
            try:
                recorded = float(row["parent_create_time"])
            except (TypeError, ValueError):
                return False
            if abs(recorded - create_time) < _CREATE_TIME_TOLERANCE_SECONDS:
                return False
        return True

    return available

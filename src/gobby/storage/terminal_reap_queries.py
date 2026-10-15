"""Machine-local terminal queries used by the stale-pending reaper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminal_settlement import TERMINAL_COLUMNS
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal


class TerminalReapQueryMixin:
    """Reaper reads that stay on this machine's terminal rows."""

    db: HubDatabase

    def list_stale_pending(self, max_age_seconds: float) -> list[Terminal]:
        """Pending rows on this machine older than the in-doubt deadline."""
        from gobby.storage.terminals import Terminal

        rows = self.db.fetchall(
            f"""
            SELECT {TERMINAL_COLUMNS} FROM terminals
            WHERE state = 'pending'
              AND machine_id = %s
              AND attempt_started_at <= now() - (%s * INTERVAL '1 second')
            ORDER BY attempt_started_at ASC
            """,
            (require_machine_id(), max_age_seconds),
        )
        return [Terminal.from_row(row) for row in rows]

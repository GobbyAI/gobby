"""Attempt-owned terminal process recording and lifecycle settlement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.storage.terminals import Terminal

ALLOWED_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("pending", "live"),
        ("pending", "exited"),
        ("live", "exited"),
        ("live", "orphaned"),
        ("orphaned", "exited"),
    }
)


class IllegalTerminalTransitionError(RuntimeError):
    """Raised when a caller requests a state edge outside the allowlist."""


@dataclass(slots=True)
class _SettlementLockCell:
    lock: asyncio.Lock
    references: int = 0


def _terminal(row: Mapping[str, Any] | None) -> Terminal | None:
    if row is None:
        return None
    from gobby.storage.terminals import Terminal

    return Terminal.from_row(row)


class TerminalSettlementMixin:
    """Storage CAS operations owned by one terminal spawn attempt."""

    db: HubDatabase

    def __init__(self) -> None:
        self._settlement_locks: dict[str, _SettlementLockCell] = {}

    @asynccontextmanager
    async def settle_lock(self, terminal_id: str) -> AsyncIterator[None]:
        cell = self._settlement_locks.get(terminal_id)
        if cell is None:
            cell = _SettlementLockCell(asyncio.Lock())
            self._settlement_locks[terminal_id] = cell
        cell.references += 1
        try:
            async with cell.lock:
                yield
        finally:
            cell.references -= 1
            if cell.references == 0 and self._settlement_locks.get(terminal_id) is cell:
                del self._settlement_locks[terminal_id]

    def fail_pending(self, terminal_id: str) -> Terminal | None:
        """CAS pending to exited for a spawn that never produced a resource."""
        return self._cas(terminal_id, expected="pending", new_state="exited")

    def mark_exited(self, terminal_id: str) -> Terminal | None:
        """CAS live or orphaned to exited without clearing locator identity."""
        live = self._cas(terminal_id, expected="live", new_state="exited")
        if live is not None:
            return live
        return self._cas(terminal_id, expected="orphaned", new_state="exited")

    def mark_orphaned(self, terminal_id: str) -> Terminal | None:
        """CAS live to orphaned after native host-epoch or host-crash loss."""
        return self._cas(terminal_id, expected="live", new_state="orphaned")

    def fail_pending_attempt(
        self,
        terminal_id: str,
        *,
        attempt_generation: int,
        attempt_started_at: datetime,
    ) -> Terminal | None:
        """CAS pending to exited only when the captured attempt still owns the row."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET state = 'exited', updated_at = now()
            WHERE id = %s
              AND state = 'pending'
              AND attempt_generation = %s
              AND attempt_started_at = %s
            RETURNING *
            """,
            (str(UUID(terminal_id)), attempt_generation, attempt_started_at),
        )
        return _terminal(row)

    def record_process(
        self,
        terminal_id: str,
        process: Mapping[str, object],
        *,
        attempt_generation: int,
        attempt_started_at: datetime,
    ) -> Terminal | None:
        """Merge native process identity only onto the captured pending attempt."""
        host_terminal_id = process.get("host_terminal_id")
        if not isinstance(host_terminal_id, str) or not host_terminal_id:
            raise ValueError("process.host_terminal_id is required")
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET process = COALESCE(process, '{}'::jsonb) || %s,
                updated_at = now()
            WHERE id = %s
              AND state = 'pending'
              AND backend = 'native'
              AND attempt_generation = %s
              AND attempt_started_at = %s
            RETURNING *
            """,
            (
                Jsonb(dict(process)),
                str(UUID(terminal_id)),
                attempt_generation,
                attempt_started_at,
            ),
        )
        return _terminal(row)

    def merge_process_reap_record(
        self,
        terminal_id: str,
        *,
        pgid: int,
        start_time: object,
    ) -> Terminal | None:
        """Merge host-observed reap identity without replacing attempt-owned keys."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET process = COALESCE(process, '{}'::jsonb) || %s,
                updated_at = now()
            WHERE id = %s AND state = 'pending' AND backend = 'native'
            RETURNING *
            """,
            (Jsonb({"pgid": pgid, "start_time": start_time}), str(UUID(terminal_id))),
        )
        return _terminal(row)

    def settle_exit(self, terminal_id: str, host_terminal_id: str) -> Terminal | None:
        """Move a matching pending or live native resource to exited."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET state = 'exited', updated_at = now()
            WHERE id = %s
              AND state IN ('pending', 'live')
              AND process ->> 'host_terminal_id' = %s
            RETURNING *
            """,
            (str(UUID(terminal_id)), host_terminal_id),
        )
        return _terminal(row)

    def bump_attempt_generation(self, terminal_id: str) -> Terminal | None:
        """Start a new pending attempt and discard the prior host resource identity."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET attempt_generation = attempt_generation + 1,
                attempt_started_at = now(),
                process = process - 'host_terminal_id',
                updated_at = now()
            WHERE id = %s AND state = 'pending'
            RETURNING *
            """,
            (str(UUID(terminal_id)),),
        )
        return _terminal(row)

    def retry_attempt_unsettled(
        self,
        terminal_id: str,
        attempt_generation: int,
    ) -> Terminal | None:
        """Start a retry only while the captured attempt owns no host resource."""
        row = self.db.fetchone(
            """
            UPDATE terminals
            SET attempt_generation = attempt_generation + 1,
                attempt_started_at = now(),
                process = process - 'host_terminal_id',
                updated_at = now()
            WHERE id = %s
              AND state = 'pending'
              AND attempt_generation = %s
              AND NOT (COALESCE(process, '{}'::jsonb) ? 'host_terminal_id')
            RETURNING *
            """,
            (str(UUID(terminal_id)), attempt_generation),
        )
        return _terminal(row)

    def _cas(
        self,
        terminal_id: str,
        *,
        expected: str,
        new_state: str,
        extra: str = "",
        extra_params: tuple[object, ...] = (),
    ) -> Terminal | None:
        if (expected, new_state) not in ALLOWED_EDGES:
            raise IllegalTerminalTransitionError(
                f"Illegal terminal transition {expected}->{new_state}"
            )
        row = self.db.fetchone(
            f"""
            UPDATE terminals
            SET state = %s,
                updated_at = now()
                {extra}
            WHERE id = %s AND state = %s
            RETURNING *
            """,
            (new_state, *extra_params, str(UUID(terminal_id)), expected),
        )
        return _terminal(row)
